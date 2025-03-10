#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from typing import Any, Dict, List, Optional, Tuple, Union

import torch
from ax.core.search_space import SearchSpaceDigest
from ax.models.torch.botorch import BotorchModel, get_rounding_func
from ax.models.torch.botorch_defaults import recommend_best_out_of_sample_point
from ax.models.torch.utils import (
    _get_X_pending_and_observed,
    get_out_of_sample_best_point_acqf,
)
from ax.models.torch_base import TorchGenResults, TorchModel, TorchOptConfig
from ax.utils.common.docutils import copy_doc
from ax.utils.common.typeutils import not_none
from botorch.acquisition.acquisition import AcquisitionFunction
from botorch.acquisition.cost_aware import InverseCostWeightedUtility
from botorch.acquisition.max_value_entropy_search import (
    qMaxValueEntropy,
    qMultiFidelityMaxValueEntropy,
)
from botorch.acquisition.utils import (
    expand_trace_observations,
    project_to_target_fidelity,
)
from botorch.exceptions.errors import UnsupportedError
from botorch.models.cost import AffineFidelityCostModel
from botorch.models.model import Model
from botorch.optim.optimize import optimize_acqf
from torch import Tensor

from .utils import subset_model


class MaxValueEntropySearch(BotorchModel):
    r"""Max-value entropy search.

    Args:
        cost_intercept: The cost intercept for the affine cost of the form
            `cost_intercept + n`, where `n` is the number of generated points.
            Only used for multi-fidelity optimzation (i.e., if fidelity_features
            are present).
        linear_truncated: If `False`, use an alternate downsampling + exponential
            decay Kernel instead of the default `LinearTruncatedFidelityKernel`
            (only relevant for multi-fidelity optimization).
        kwargs: Model-specific kwargs.
    """

    def __init__(
        self,
        cost_intercept: float = 1.0,
        linear_truncated: bool = True,
        use_input_warping: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            best_point_recommender=recommend_best_out_of_sample_point,
            linear_truncated=linear_truncated,
            use_input_warping=use_input_warping,
            **kwargs,
        )
        self.cost_intercept = cost_intercept

    @copy_doc(TorchModel.gen)
    def gen(
        self,
        n: int,
        search_space_digest: SearchSpaceDigest,
        torch_opt_config: TorchOptConfig,
    ) -> TorchGenResults:
        if (
            torch_opt_config.linear_constraints is not None
            or torch_opt_config.outcome_constraints is not None
        ):
            raise UnsupportedError(
                "Constraints are not yet supported by max-value entropy search!"
            )

        if len(torch_opt_config.objective_weights) > 1:
            raise UnsupportedError(
                "Models with multiple outcomes are not yet supported by MES!"
            )

        options = torch_opt_config.model_gen_options or {}
        acf_options = options.get("acquisition_function_kwargs", {})
        optimizer_options = options.get("optimizer_kwargs", {})

        X_pending, X_observed = _get_X_pending_and_observed(
            Xs=self.Xs,
            objective_weights=torch_opt_config.objective_weights,
            bounds=search_space_digest.bounds,
            pending_observations=torch_opt_config.pending_observations,
            outcome_constraints=torch_opt_config.outcome_constraints,
            linear_constraints=torch_opt_config.linear_constraints,
            fixed_features=torch_opt_config.fixed_features,
        )

        model = self.model

        # subset model only to the outcomes we need for the optimization
        if options.get("subset_model", True):
            subset_model_results = subset_model(
                model=model,
                objective_weights=torch_opt_config.objective_weights,
                outcome_constraints=torch_opt_config.outcome_constraints,
            )
            model = subset_model_results.model
            objective_weights = subset_model_results.objective_weights
        else:
            objective_weights = torch_opt_config.objective_weights

        # get the acquisition function
        num_fantasies = acf_options.get("num_fantasies", 16)
        num_mv_samples = acf_options.get("num_mv_samples", 10)
        num_y_samples = acf_options.get("num_y_samples", 128)
        candidate_size = acf_options.get("candidate_size", 1000)
        num_restarts = optimizer_options.get("num_restarts", 40)
        raw_samples = optimizer_options.get("raw_samples", 1024)

        # generate the discrete points in the design space to sample max values
        bounds_ = torch.tensor(
            search_space_digest.bounds, dtype=self.dtype, device=self.device
        )
        bounds_ = bounds_.transpose(0, 1)

        candidate_set = torch.rand(candidate_size, bounds_.size(1))
        candidate_set = bounds_[0] + (bounds_[1] - bounds_[0]) * candidate_set

        target_fidelities = {
            k: v
            for k, v in search_space_digest.target_values.items()
            if k in search_space_digest.fidelity_features
        }
        acq_function = _instantiate_MES(
            model=model,
            candidate_set=candidate_set,
            num_fantasies=num_fantasies,
            num_trace_observations=options.get("num_trace_observations", 0),
            num_mv_samples=num_mv_samples,
            num_y_samples=num_y_samples,
            X_pending=X_pending,
            maximize=True if objective_weights[0] == 1 else False,
            target_fidelities=target_fidelities,
            fidelity_weights=options.get("fidelity_weights"),
            cost_intercept=self.cost_intercept,
        )

        # optimize and get new points
        botorch_rounding_func = get_rounding_func(torch_opt_config.rounding_func)
        opt_options: Dict[str, Union[bool, float, int, str]] = {
            "batch_limit": 8,
            "maxiter": 200,
            "method": "L-BFGS-B",
            "nonnegative": False,
        }
        opt_options.update(optimizer_options.get("options", {}))
        candidates, _ = optimize_acqf(
            acq_function=acq_function,
            bounds=bounds_,
            q=n,
            inequality_constraints=None,
            fixed_features=torch_opt_config.fixed_features,
            post_processing_func=botorch_rounding_func,
            num_restarts=num_restarts,
            raw_samples=raw_samples,
            options=opt_options,
            sequential=True,
        )
        return TorchGenResults(
            points=candidates.detach().cpu(),
            weights=torch.ones(n, dtype=self.dtype),
        )

    def _get_best_point_acqf(
        self,
        X_observed: Tensor,
        objective_weights: Tensor,
        mc_samples: int = 512,
        fixed_features: Optional[Dict[int, float]] = None,
        target_fidelities: Optional[Dict[int, float]] = None,
        outcome_constraints: Optional[Tuple[Tensor, Tensor]] = None,
        seed_inner: Optional[int] = None,
        qmc: bool = True,
        **kwargs: Any,
    ) -> Tuple[AcquisitionFunction, Optional[List[int]]]:
        # `outcome_constraints` is validated to be None in `gen`
        if outcome_constraints is not None:
            raise UnsupportedError("Outcome constraints not yet supported.")

        return get_out_of_sample_best_point_acqf(
            model=not_none(self.model),
            Xs=self.Xs,
            objective_weights=objective_weights,
            # With None `outcome_constraints`, `get_objective` utility
            # always returns a `ScalarizedObjective`, which results in
            # `get_out_of_sample_best_point_acqf` always selecting
            # `PosteriorMean`.
            outcome_constraints=outcome_constraints,
            X_observed=not_none(X_observed),
            seed_inner=seed_inner,
            fixed_features=fixed_features,
            fidelity_features=self.fidelity_features,
            target_fidelities=target_fidelities,
            qmc=qmc,
        )


def _instantiate_MES(
    model: Model,
    candidate_set: Tensor,
    num_fantasies: int = 16,
    num_mv_samples: int = 10,
    num_y_samples: int = 128,
    use_gumbel: bool = True,
    X_pending: Optional[Tensor] = None,
    maximize: bool = True,
    num_trace_observations: int = 0,
    target_fidelities: Optional[Dict[int, float]] = None,
    fidelity_weights: Optional[Dict[int, float]] = None,
    cost_intercept: float = 1.0,
) -> qMaxValueEntropy:
    if target_fidelities:
        if fidelity_weights is None:
            fidelity_weights = {f: 1.0 for f in target_fidelities}
        if not set(target_fidelities) == set(fidelity_weights):
            raise RuntimeError(
                "Must provide the same indices for target_fidelities "
                f"({set(target_fidelities)}) and fidelity_weights "
                f" ({set(fidelity_weights)})."
            )
        cost_model = AffineFidelityCostModel(
            fidelity_weights=fidelity_weights, fixed_cost=cost_intercept
        )
        cost_aware_utility = InverseCostWeightedUtility(cost_model=cost_model)

        def project(X: Tensor) -> Tensor:
            return project_to_target_fidelity(X=X, target_fidelities=target_fidelities)

        def expand(X: Tensor) -> Tensor:
            return expand_trace_observations(
                X=X,
                fidelity_dims=sorted(target_fidelities),  # pyre-ignore: [6]
                num_trace_obs=num_trace_observations,
            )

        return qMultiFidelityMaxValueEntropy(
            model=model,
            candidate_set=candidate_set,
            num_fantasies=num_fantasies,
            num_mv_samples=num_mv_samples,
            num_y_samples=num_y_samples,
            X_pending=X_pending,
            maximize=maximize,
            cost_aware_utility=cost_aware_utility,
            project=project,
            expand=expand,
        )

    return qMaxValueEntropy(
        model=model,
        candidate_set=candidate_set,
        num_fantasies=num_fantasies,
        num_mv_samples=num_mv_samples,
        num_y_samples=num_y_samples,
        X_pending=X_pending,
        maximize=maximize,
    )
