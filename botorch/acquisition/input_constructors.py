#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

r"""
A registry of helpers for generating inputs to acquisition function
constructors programmatically from a consistent input format.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple, Type, Union

import torch
from botorch.acquisition.acquisition import AcquisitionFunction
from botorch.acquisition.analytic import (
    ExpectedImprovement,
    PosteriorMean,
    ProbabilityOfImprovement,
    UpperConfidenceBound,
    ConstrainedExpectedImprovement,
    NoisyExpectedImprovement,
)
from botorch.acquisition.cost_aware import InverseCostWeightedUtility
from botorch.acquisition.fixed_feature import FixedFeatureAcquisitionFunction
from botorch.acquisition.knowledge_gradient import (
    qKnowledgeGradient,
    qMultiFidelityKnowledgeGradient,
)
from botorch.acquisition.max_value_entropy_search import (
    qMaxValueEntropy,
    qMultiFidelityMaxValueEntropy,
)
from botorch.acquisition.monte_carlo import (
    qExpectedImprovement,
    qNoisyExpectedImprovement,
    qProbabilityOfImprovement,
    qSimpleRegret,
    qUpperConfidenceBound,
)
from botorch.acquisition.multi_objective import (
    ExpectedHypervolumeImprovement,
    qExpectedHypervolumeImprovement,
    qNoisyExpectedHypervolumeImprovement,
)
from botorch.acquisition.multi_objective.objective import (
    IdentityAnalyticMultiOutputObjective,
    IdentityMCMultiOutputObjective,
)
from botorch.acquisition.multi_objective.utils import get_default_partitioning_alpha
from botorch.acquisition.objective import (
    AcquisitionObjective,
    IdentityMCObjective,
    ScalarizedObjective,
    MCAcquisitionObjective,
)
from botorch.acquisition.utils import (
    expand_trace_observations,
    project_to_target_fidelity,
)
from botorch.exceptions.errors import UnsupportedError
from botorch.models.cost import AffineFidelityCostModel
from botorch.models.model import Model
from botorch.optim.optimize import optimize_acqf
from botorch.sampling.samplers import IIDNormalSampler, MCSampler, SobolQMCNormalSampler
from botorch.utils.constraints import get_outcome_constraint_transforms
from botorch.utils.containers import TrainingData
from botorch.utils.multi_objective.box_decompositions.non_dominated import (
    FastNondominatedPartitioning,
    NondominatedPartitioning,
)
from torch import Tensor


ACQF_INPUT_CONSTRUCTOR_REGISTRY = {}


def get_acqf_input_constructor(
    acqf_cls: Type[AcquisitionFunction],
) -> Callable[..., Dict[str, Any]]:
    r"""Get acqusition function input constructor from registry.

    Args:
        acqf_cls: The AcquisitionFunction class (not instance) for which
            to retrieve the input constructor.

    Returns:
        The input constructor associated with `acqf_cls`.

    """
    if acqf_cls not in ACQF_INPUT_CONSTRUCTOR_REGISTRY:
        raise RuntimeError(
            f"Input constructor for acquisition class `{acqf_cls.__name__}` not "
            "registered. Use the `@acqf_input_constructor` decorator to register "
            "a new method."
        )
    return ACQF_INPUT_CONSTRUCTOR_REGISTRY[acqf_cls]


def acqf_input_constructor(
    *acqf_cls: Type[AcquisitionFunction],
) -> Callable[..., AcquisitionFunction]:
    r"""Decorator for registering acquisition function input constructors.

    Args:
        acqf_cls: The AcquisitionFunction classes (not instances) for which
            to register the input constructor.
    """
    for acqf_cls_ in acqf_cls:
        if acqf_cls_ in ACQF_INPUT_CONSTRUCTOR_REGISTRY:
            raise ValueError(
                "Cannot register duplicate arg constructor for acquisition "
                f"class `{acqf_cls_.__name__}`"
            )

    def decorator(method):
        for acqf_cls_ in acqf_cls:
            _register_acqf_input_constructor(
                acqf_cls=acqf_cls_, input_constructor=method
            )
            ACQF_INPUT_CONSTRUCTOR_REGISTRY[acqf_cls_] = method
        return method

    return decorator


def _register_acqf_input_constructor(
    acqf_cls: Type[AcquisitionFunction],
    input_constructor: Callable[..., Dict[str, Any]],
) -> None:
    ACQF_INPUT_CONSTRUCTOR_REGISTRY[acqf_cls] = input_constructor


# --------------------- Input argument constructors --------------------- #


@acqf_input_constructor(PosteriorMean)
def construct_inputs_analytic_base(
    model: Model,
    training_data: TrainingData,
    objective: Optional[AcquisitionObjective] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    r"""Construct kwargs for basic analytic acquisition functions.

    Args:
        model: The model to be used in the acquisition function.
        training_data: A TrainingData object contraining the model's
            training data. `best_f` is extracted from here.
        objective: The objective to in the acquisition function.

    Returns:
        A dict mapping kwarg names of the constructor to values.
    """
    return {"model": model, "objective": objective}


@acqf_input_constructor(ExpectedImprovement, ProbabilityOfImprovement)
def construct_inputs_best_f(
    model: Model,
    training_data: TrainingData,
    objective: Optional[AcquisitionObjective] = None,
    maximize: bool = True,
    **kwargs: Any,
) -> Dict[str, Any]:
    r"""Construct kwargs for the acquisition functions requiring `best_f`.

    Args:
        model: The model to be used in the acquisition function.
        training_data: A TrainingData object contraining the model's
            training data. `best_f` is extracted from here.
        objective: The objective to in the acquisition function.
        maximize: If True, consider the problem a maximization problem.

    Returns:
        A dict mapping kwarg names of the constructor to values.
    """
    base_inputs = construct_inputs_analytic_base(
        model=model, training_data=training_data, objective=objective
    )
    best_f = kwargs.get(
        "best_f", get_best_f_analytic(training_data=training_data, objective=objective)
    )
    return {**base_inputs, "best_f": best_f, "maximize": maximize}


@acqf_input_constructor(UpperConfidenceBound)
def construct_inputs_ucb(
    model: Model,
    training_data: TrainingData,
    objective: Optional[AcquisitionObjective] = None,
    beta: Union[float, Tensor] = 0.2,
    maximize: bool = True,
    **kwargs: Any,
) -> Dict[str, Any]:
    r"""Construct kwargs for `UpperConfidenceBound`.

    Args:
        model: The model to be used in the acquisition function.
        training_data: A TrainingData object contraining the model's
            training data. `best_f` is extracted from here.
        objective: The objective to in the acquisition function.
        beta: Either a scalar or a one-dim tensor with `b` elements (batch mode)
            representing the trade-off parameter between mean and covariance
        maximize: If True, consider the problem a maximization problem.

    Returns:
        A dict mapping kwarg names of the constructor to values.
    """
    base_inputs = construct_inputs_analytic_base(
        model=model, training_data=training_data, objective=objective
    )
    return {**base_inputs, "beta": beta, "maximize": maximize}


@acqf_input_constructor(ConstrainedExpectedImprovement)
def construct_inputs_constrained_ei(
    model: Model,
    training_data: TrainingData,
    objective_index: int,
    constraints: Dict[int, Tuple[Optional[float], Optional[float]]],
    maximize: bool = True,
    **kwargs: Any,
) -> Dict[str, Any]:
    r"""Construct kwargs for `ConstrainedExpectedImprovement`.

    Args:
        model: The model to be used in the acquisition function.
        training_data: A TrainingData object contraining the model's
            training data. `best_f` is extracted from here.
        objective_index: The index of the objective.
        constraints: A dictionary of the form `{i: [lower, upper]}`, where
            `i` is the output index, and `lower` and `upper` are lower and upper
            bounds on that output (resp. interpreted as -Inf / Inf if None)
        maximize: If True, consider the problem a maximization problem.

    Returns:
        A dict mapping kwarg names of the constructor to values.
    """
    # TODO: Implement best point computation from training data
    # best_f =
    # return {
    #     "model": model,
    #     "best_f": best_f,
    #     "objective_index": objective_index,
    #     "constraints": constraints,
    #     "maximize": maximize,
    # }
    raise NotImplementedError  # pragma: nocover


@acqf_input_constructor(NoisyExpectedImprovement)
def construct_inputs_noisy_ei(
    model: Model,
    training_data: TrainingData,
    num_fantasies: int = 20,
    maximize: bool = True,
    **kwargs: Any,
) -> Dict[str, Any]:
    r"""Construct kwargs for `NoisyExpectedImprovement`.

    Args:
        model: The model to be used in the acquisition function.
        training_data: A TrainingData object contraining the model's
            training data. `best_f` is extracted from here.
        num_fantasies: The number of fantasies to generate. The higher this
            number the more accurate the model (at the expense of model
            complexity and performance).
        maximize: If True, consider the problem a maximization problem.

    Returns:
        A dict mapping kwarg names of the constructor to values.
    """
    # TODO: Add prune_baseline functionality as for qNEI
    if not training_data.is_block_design:
        raise NotImplementedError("Currently only block designs are supported")
    return {
        "model": model,
        "X_observed": training_data.X,
        "num_fantasies": num_fantasies,
        "maximize": maximize,
    }


@acqf_input_constructor(qSimpleRegret)
def construct_inputs_mc_base(
    model: Model,
    training_data: TrainingData,
    objective: Optional[AcquisitionObjective] = None,
    X_pending: Optional[Tensor] = None,
    sampler: Optional[MCSampler] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    r"""Construct kwargs for basic MC acquisition functions.

    Args:
        model: The model to be used in the acquisition function.
        training_data: A TrainingData object contraining the model's
            training data. Used e.g. to extract inputs such as `best_f`
            for expected improvement acquisition functions.
        objective: The objective to in the acquisition function.
        X_pending: A `batch_shape, m x d`-dim Tensor of `m` design points
            that have points that have been submitted for function evaluation
            but have not yet been evaluated.
        sampler: The sampler used to draw base samples. If omitted, uses
            the acquisition functions's default sampler.

    Returns:
        A dict mapping kwarg names of the constructor to values.
    """
    return {
        "model": model,
        "objective": objective,
        "X_pending": X_pending,
        "sampler": sampler,
    }


@acqf_input_constructor(qExpectedImprovement)
def construct_inputs_qEI(
    model: Model,
    training_data: TrainingData,
    objective: Optional[AcquisitionObjective] = None,
    X_pending: Optional[Tensor] = None,
    sampler: Optional[MCSampler] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    r"""Construct kwargs for the `qExpectedImprovement` constructor.

    Args:
        model: The model to be used in the acquisition function.
        training_data: A TrainingData object contraining the model's
            training data. Used e.g. to extract inputs such as `best_f`
            for expected improvement acquisition functions.
        objective: The objective to in the acquisition function.
        X_pending: A `m x d`-dim Tensor of `m` design points that have been
            submitted for function evaluation but have not yet been evaluated.
            Concatenated into X upon forward call.
        sampler: The sampler used to draw base samples. If omitted, uses
            the acquisition functions's default sampler.

    Returns:
        A dict mapping kwarg names of the constructor to values.
    """
    base_inputs = construct_inputs_mc_base(
        model=model,
        training_data=training_data,
        objective=objective,
        sampler=sampler,
        X_pending=X_pending,
    )
    # TODO: Dedup handling of this here and in the constructor (maybe via a
    # shared classmethod doing this)
    best_f = kwargs.get(
        "best_f", get_best_f_mc(training_data=training_data, objective=objective)
    )
    return {**base_inputs, "best_f": best_f}


@acqf_input_constructor(qNoisyExpectedImprovement)
def construct_inputs_qNEI(
    model: Model,
    training_data: TrainingData,
    objective: Optional[AcquisitionObjective] = None,
    X_pending: Optional[Tensor] = None,
    sampler: Optional[MCSampler] = None,
    X_baseline: Optional[Tensor] = None,
    prune_baseline: bool = False,
    **kwargs: Any,
) -> Dict[str, Any]:
    r"""Construct kwargs for the `qNoisyExpectedImprovement` constructor.

    Args:
        model: The model to be used in the acquisition function.
        training_data: A TrainingData object contraining the model's
            training data. Used e.g. to extract inputs such as `best_f`
            for expected improvement acquisition functions. Only block-
            design training data currently supported.
        objective: The objective to in the acquisition function.
        X_pending: A `m x d`-dim Tensor of `m` design points that have been
            submitted for function evaluation but have not yet been evaluated.
            Concatenated into X upon forward call.
        sampler: The sampler used to draw base samples. If omitted, uses
            the acquisition functions's default sampler.
        X_baseline: A `batch_shape x r x d`-dim Tensor of `r` design points
            that have already been observed. These points are considered as
            the potential best design point. If omitted, use `training_data.X`.
        prune_baseline: If True, remove points in `X_baseline` that are
            highly unlikely to be the best point. This can significantly
            improve performance and is generally recommended.

    Returns:
        A dict mapping kwarg names of the constructor to values.
    """
    base_inputs = construct_inputs_mc_base(
        model=model,
        training_data=training_data,
        objective=objective,
        sampler=sampler,
        X_pending=X_pending,
    )
    if X_baseline is None:
        if not training_data.is_block_design:
            raise NotImplementedError("Currently only block designs are supported.")
        X_baseline = training_data.X
    return {
        **base_inputs,
        "X_baseline": X_baseline,
        "prune_baseline": prune_baseline,
    }


@acqf_input_constructor(qProbabilityOfImprovement)
def construct_inputs_qPI(
    model: Model,
    training_data: TrainingData,
    objective: Optional[AcquisitionObjective] = None,
    X_pending: Optional[Tensor] = None,
    sampler: Optional[MCSampler] = None,
    tau: float = 1e-3,
    best_f: Optional[Union[float, Tensor]] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    r"""Construct kwargs for the `qProbabilityOfImprovement` constructor.

    Args:
        model: The model to be used in the acquisition function.
        training_data: A TrainingData object contraining the model's
            training data. Used e.g. to extract inputs such as `best_f`
            for expected improvement acquisition functions.
        objective: The objective to in the acquisition function.
        X_pending: A `m x d`-dim Tensor of `m` design points that have been
            submitted for function evaluation but have not yet been evaluated.
            Concatenated into X upon forward call.
        sampler: The sampler used to draw base samples. If omitted, uses
            the acquisition functions's default sampler.
        tau: The temperature parameter used in the sigmoid approximation
            of the step function. Smaller values yield more accurate
            approximations of the function, but result in gradients
            estimates with higher variance.
        best_f: The best objective value observed so far (assumed noiseless). Can
            be a `batch_shape`-shaped tensor, which in case of a batched model
            specifies potentially different values for each element of the batch.
    Returns:
        A dict mapping kwarg names of the constructor to values.
    """
    base_inputs = construct_inputs_mc_base(
        model=model,
        training_data=training_data,
        objective=objective,
        sampler=sampler,
        X_pending=X_pending,
    )
    # TODO: Dedup handling of this here and in the constructor (maybe via a
    # shared classmethod doing this)
    if best_f is None:
        best_f = get_best_f_mc(training_data=training_data, objective=objective)
    return {
        **base_inputs,
        "tau": tau,
        "best_f": best_f,
    }


@acqf_input_constructor(qUpperConfidenceBound)
def construct_inputs_qUCB(
    model: Model,
    training_data: TrainingData,
    objective: Optional[AcquisitionObjective] = None,
    X_pending: Optional[Tensor] = None,
    sampler: Optional[MCSampler] = None,
    beta: float = 0.2,
    **kwargs: Any,
) -> Dict[str, Any]:
    r"""Construct kwargs for the `qUpperConfidenceBound` constructor.

    Args:
        model: The model to be used in the acquisition function.
        training_data: A TrainingData object contraining the model's
            training data. Used e.g. to extract inputs such as `best_f`
            for expected improvement acquisition functions.
        objective: The objective to in the acquisition function.
        X_pending: A `m x d`-dim Tensor of `m` design points that have been
            submitted for function evaluation but have not yet been evaluated.
            Concatenated into X upon forward call.
        sampler: The sampler used to draw base samples. If omitted, uses
            the acquisition functions's default sampler.
        beta: Controls tradeoff between mean and standard deviation in UCB.

    Returns:
        A dict mapping kwarg names of the constructor to values.
    """
    base_inputs = construct_inputs_mc_base(
        model=model,
        training_data=training_data,
        objective=objective,
        sampler=sampler,
        X_pending=X_pending,
    )
    return {**base_inputs, "beta": beta}


def _get_sampler(mc_samples: int, qmc: bool) -> MCSampler:
    """Set up MC sampler for q(N)EHVI."""
    # initialize the sampler
    seed = int(torch.randint(1, 10000, (1,)).item())
    if qmc:
        return SobolQMCNormalSampler(num_samples=mc_samples, seed=seed)
    return IIDNormalSampler(num_samples=mc_samples, seed=seed)


@acqf_input_constructor(ExpectedHypervolumeImprovement)
def construct_inputs_EHVI(
    model: Model,
    training_data: TrainingData,
    objective_thresholds: Tensor,
    objective: Optional[AcquisitionObjective] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    r"""Construct kwargs for `ExpectedHypervolumeImprovement` constructor."""
    num_objectives = objective_thresholds.shape[0]
    if kwargs.get("outcome_constraints") is not None:
        raise NotImplementedError("EHVI does not yet support outcome constraints.")

    X_observed = training_data.X
    alpha = kwargs.get(
        "alpha",
        get_default_partitioning_alpha(num_objectives=num_objectives),
    )
    # This selects the objectives (a subset of the outcomes) and set each
    # objective threhsold to have the proper optimization direction.
    if objective is None:
        objective = IdentityAnalyticMultiOutputObjective()
    ref_point = objective(objective_thresholds)

    # Compute posterior mean (for ref point computation ref pareto frontier)
    # if one is not provided among arguments.
    Y_pmean = kwargs.get("Y_pmean")
    if Y_pmean is None:
        with torch.no_grad():
            Y_pmean = model.posterior(X_observed).mean
    if alpha > 0:
        partitioning = NondominatedPartitioning(
            ref_point=ref_point,
            Y=objective(Y_pmean),
            alpha=alpha,
        )
    else:
        partitioning = FastNondominatedPartitioning(
            ref_point=ref_point,
            Y=objective(Y_pmean),
        )

    return {
        "model": model,
        "ref_point": ref_point,
        "partitioning": partitioning,
        "objective": objective,
    }


@acqf_input_constructor(qExpectedHypervolumeImprovement)
def construct_inputs_qEHVI(
    model: Model,
    training_data: TrainingData,
    objective_thresholds: Tensor,
    objective: Optional[AcquisitionObjective] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    r"""Construct kwargs for `qExpectedHypervolumeImprovement` constructor."""
    X_observed = training_data.X

    # compute posterior mean (for ref point computation ref pareto frontier)
    with torch.no_grad():
        Y_pmean = model.posterior(X_observed).mean

    outcome_constraints = kwargs.pop("outcome_constraints", None)
    # For HV-based acquisition functions we pass the constraint transform directly
    if outcome_constraints is None:
        cons_tfs = None
    else:
        cons_tfs = get_outcome_constraint_transforms(outcome_constraints)
        # Adjust `Y_pmean` to contrain feasible points only.
        feas = torch.stack([c(Y_pmean) <= 0 for c in cons_tfs], dim=-1).all(dim=-1)
        Y_pmean = Y_pmean[feas]

    if objective is None:
        objective = IdentityMCMultiOutputObjective()

    ehvi_kwargs = construct_inputs_EHVI(
        model=model,
        training_data=training_data,
        objective_thresholds=objective_thresholds,
        objective=objective,
        # Pass `Y_pmean` that accounts for constraints to `construct_inputs_EHVI`
        # to ensure that correct non-dominated partitioning is produced.
        Y_pmean=Y_pmean,
        **kwargs,
    )

    sampler = kwargs.get("sampler")
    if sampler is None:
        sampler = _get_sampler(
            mc_samples=kwargs.get("mc_samples", 128), qmc=kwargs.get("qmc", True)
        )

    add_qehvi_kwargs = {
        "sampler": sampler,
        "X_pending": kwargs.get("X_pending"),
        "constraints": cons_tfs,
        "eta": kwargs.get("eta", 1e-3),
    }
    return {**ehvi_kwargs, **add_qehvi_kwargs}


@acqf_input_constructor(qNoisyExpectedHypervolumeImprovement)
def construct_inputs_qNEHVI(
    model: Model,
    training_data: TrainingData,
    objective_thresholds: Tensor,
    objective: Optional[AcquisitionObjective] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    r"""Construct kwargs for `qNoisyExpectedHypervolumeImprovement` constructor."""
    # This selects the objectives (a subset of the outcomes) and set each
    # objective threhsold to have the proper optimization direction.
    if objective is None:
        objective = IdentityMCMultiOutputObjective()

    outcome_constraints = kwargs.pop("outcome_constraints", None)
    if outcome_constraints is None:
        cons_tfs = None
    else:
        cons_tfs = get_outcome_constraint_transforms(outcome_constraints)

    sampler = kwargs.get("sampler")
    if sampler is None:
        sampler = _get_sampler(
            mc_samples=kwargs.get("mc_samples", 128), qmc=kwargs.get("qmc", True)
        )

    return {
        "model": model,
        "ref_point": objective(objective_thresholds),
        "X_baseline": kwargs.get("X_baseline", training_data.X),
        "sampler": sampler,
        "objective": objective,
        "constraints": cons_tfs,
        "X_pending": kwargs.get("X_pending"),
        "eta": kwargs.get("eta", 1e-3),
        "prune_baseline": kwargs.get("prune_baseline", True),
        "alpha": kwargs.get("alpha", 0.0),
        "cache_pending": kwargs.get("cache_pending", True),
        "max_iep": kwargs.get("max_iep", 0),
        "incremental_nehvi": kwargs.get("incremental_nehvi", True),
    }


@acqf_input_constructor(qMaxValueEntropy)
def construct_inputs_qMES(
    model: Model,
    training_data: TrainingData,
    objective: AcquisitionObjective,
    bounds: List[Tuple[float, float]],
    candidate_size: int = 1000,
    **kwargs: Any,
) -> Dict[str, Any]:
    r"""Construct kwargs for `qMaxValueEntropy` constructor."""
    inputs_mc = construct_inputs_mc_base(
        model=model,
        training_data=training_data,
        objective=objective,
        **kwargs,
    )

    _kw = {"dtype": training_data.X.dtype, "device": training_data.X.device}
    _rvs = torch.rand(candidate_size, len(bounds), **_kw)
    _bounds = torch.tensor(bounds, **_kw).transpose(0, 1)
    return {
        **inputs_mc,
        "candidate_set": _bounds[0] + (_bounds[1] - _bounds[0]) * _rvs,
        "maximize": kwargs.get("maximize", True),
    }


def construct_inputs_mf_base(
    model: Model,
    training_data: TrainingData,
    target_fidelities: Dict[int, Union[int, float]],
    fidelity_weights: Optional[Dict[int, float]] = None,
    cost_intercept: float = 1.0,
    num_trace_observations: int = 0,
    **ignore: Any,
) -> Dict[str, Any]:
    r"""Construct kwargs for a multifidetlity acquisition function's constructor."""
    if fidelity_weights is None:
        fidelity_weights = {f: 1.0 for f in target_fidelities}

    if set(target_fidelities) != set(fidelity_weights):
        raise RuntimeError(
            "Must provide the same indices for target_fidelities "
            f"({set(target_fidelities)}) and fidelity_weights "
            f" ({set(fidelity_weights)})."
        )

    cost_aware_utility = InverseCostWeightedUtility(
        cost_model=AffineFidelityCostModel(
            fidelity_weights=fidelity_weights, fixed_cost=cost_intercept
        )
    )

    return {
        "target_fidelities": target_fidelities,
        "cost_aware_utility": cost_aware_utility,
        "expand": lambda X: expand_trace_observations(
            X=X,
            fidelity_dims=sorted(target_fidelities),
            num_trace_obs=num_trace_observations,
        ),
        "project": lambda X: project_to_target_fidelity(
            X=X, target_fidelities=target_fidelities
        ),
    }


@acqf_input_constructor(qKnowledgeGradient)
def construct_inputs_qKG(
    model: Model,
    training_data: TrainingData,
    objective: AcquisitionObjective,
    bounds: List[Tuple[float, float]],
    target_fidelities: Optional[Dict[int, float]] = None,
    num_fantasies: int = 64,
    **kwargs: Any,
) -> Dict[str, Any]:
    r"""Construct kwargs for `qKnowledgeGradient` constructor."""

    inputs_mc = construct_inputs_mc_base(
        model=model,
        training_data=training_data,
        objective=objective,
        **kwargs,
    )

    _bounds = torch.tensor(
        data=bounds,
        dtype=training_data.X.dtype,
        device=training_data.X.device,
    )

    _, current_value = optimize_objective(
        model=model,
        objective=objective,
        bounds=_bounds.t(),
        q=1,
        target_fidelities=target_fidelities,
        **kwargs,
    )

    return {
        **inputs_mc,
        "num_fantasies": num_fantasies,
        "current_value": current_value.detach().cpu().max(),
    }


@acqf_input_constructor(qMultiFidelityKnowledgeGradient)
def construct_inputs_qMFKG(
    model: Model,
    training_data: TrainingData,
    objective: AcquisitionObjective,
    bounds: List[Tuple[float, float]],
    target_fidelities: Dict[int, Union[int, float]],
    **kwargs: Any,
) -> Dict[str, Any]:
    r"""Construct kwargs for `qMultiFidelityKnowledgeGradient` constructor."""

    inputs_mf = construct_inputs_mf_base(
        model=model,
        training_data=training_data,
        objective=objective,
        bounds=bounds,
        target_fidelities=target_fidelities,
        **kwargs,
    )

    inputs_kg = construct_inputs_qKG(
        model=model,
        training_data=training_data,
        objective=objective,
        bounds=bounds,
        **kwargs,
    )

    return {**inputs_mf, **inputs_kg}


@acqf_input_constructor(qMultiFidelityMaxValueEntropy)
def construct_inputs_qMFMES(
    model: Model,
    training_data: TrainingData,
    objective: AcquisitionObjective,
    bounds: List[Tuple[float, float]],
    target_fidelities: Dict[int, Union[int, float]],
    **kwargs: Any,
) -> Dict[str, Any]:
    r"""Construct kwargs for `qMultiFidelityMaxValueEntropy` constructor."""
    inputs_mf = construct_inputs_mf_base(
        model=model,
        training_data=training_data,
        objective=objective,
        target_fidelities=target_fidelities,
        bounds=bounds,
        **kwargs,
    )

    inputs_qmes = construct_inputs_qMES(
        model=model,
        training_data=training_data,
        objective=objective,
        bounds=bounds,
        **kwargs,
    )

    _bounds = torch.tensor(
        data=bounds,
        dtype=training_data.X.dtype,
        device=training_data.X.device,
    )

    _, current_value = optimize_objective(
        model=model,
        objective=objective,
        bounds=_bounds.t(),
        q=1,
        target_fidelities=target_fidelities,
        **kwargs,
    )

    return {
        **inputs_mf,
        **inputs_qmes,
        "current_value": current_value.detach().cpu().max(),
    }


def get_best_f_analytic(
    training_data: TrainingData,
    objective: Optional[AcquisitionObjective] = None,
) -> Tensor:
    if not training_data.is_block_design:
        raise NotImplementedError("Currently only block designs are supported.")
    Y = training_data.Y
    if isinstance(objective, ScalarizedObjective):
        return objective.evaluate(Y).max(-1).values
    if Y.shape[-1] > 1:
        raise NotImplementedError(
            "Analytic acquisition functions currently only work with "
            "multi-output models if provided with a `ScalarizedObjective`."
        )
    return Y.max(-2).values.squeeze(-1)


def get_best_f_mc(
    training_data: TrainingData,
    objective: Optional[AcquisitionObjective] = None,
) -> Tensor:
    if not training_data.is_block_design:
        raise NotImplementedError("Currently only block designs are supported.")
    Y = training_data.Y
    if objective is None:
        if Y.shape[-1] > 1:
            raise UnsupportedError(
                "Acquisition functions require an objective when "
                "used with multi-output models (execpt for multi-objective"
                "acquisition functions)."
            )
        objective = IdentityMCObjective()
    return objective(training_data.Y).max(-1).values


def optimize_objective(
    model: Model,
    objective: Union[ScalarizedObjective, MCAcquisitionObjective],
    bounds: Tensor,
    q: int,
    linear_constraints: Optional[Tuple[Tensor, Tensor]] = None,
    fixed_features: Optional[Dict[int, float]] = None,
    target_fidelities: Optional[Dict[int, float]] = None,
    qmc: bool = True,
    mc_samples: int = 512,
    seed_inner: Optional[int] = None,
    optimizer_options: Dict[str, Any] = None,
    post_processing_func: Optional[Callable[[Tensor], Tensor]] = None,
    batch_initial_conditions: Optional[Tensor] = None,
    sequential: bool = False,
    **ignore,
) -> Tuple[Tensor, Tensor]:
    r"""Optimize an objective under the given model.

    Args:
        model: The model to be used in the objective.
        objective: The objective to optimize.
        bounds: A `2 x d` tensor of lower and upper bounds for each column of `X`.
        q: The cardinality of input sets on which the objective is to be evaluated.
        linear_constraints: A tuple of (A, b). Given `k` linear constraints on a
            `d`-dimensional space, `A` is `k x d` and `b` is `k x 1` such that
            `A x <= b`. (Not used by single task models).
        fixed_features: A dictionary of feature assignments `{feature_index: value}` to
            hold fixed during generation.
        target_fidelities: A dictionary mapping input feature indices to fidelity
            values. Defaults to `{-1: 1.0}`.
        qmc: Toggle for enabling (qmc=1) or disabling (qmc=0) use of Quasi Monte Carlo.
        mc_samples: Integer number of samples used to estimate Monte Carlo objectives.
        seed_inner: Integer seed used to initialize the sampler passed to MCObjective.
        optimizer_options: Table used to lookup keyword arguments for the optimizer.
        post_processing_func: A function that post-processes an optimization
            result appropriately (i.e. according to `round-trip` transformations).
        batch_initial_conditions: A Tensor of initial values for the optimizer.
        sequential: If False, uses joint optimization, otherwise uses sequential
            optimization.

    Returns:
        A tuple of <torch.Tensor> containing the best input locations and
        corresponding objective values.
    """
    if optimizer_options is None:
        optimizer_options = {}

    if isinstance(objective, MCAcquisitionObjective):
        sampler_cls = SobolQMCNormalSampler if qmc else IIDNormalSampler
        acqf_cls = qSimpleRegret
        acqf_opt = {"sampler": sampler_cls(num_samples=mc_samples, seed=seed_inner)}
    else:
        acqf_cls = PosteriorMean
        acqf_opt = {}

    acq_function = acqf_cls(model=model, objective=objective, **acqf_opt)
    if fixed_features:
        acq_function = FixedFeatureAcquisitionFunction(
            acq_function=acq_function,
            d=bounds.shape[-1],
            columns=list(fixed_features.keys()),
            values=list(fixed_features.values()),
        )
        free_feature_dims = list(range(len(bounds)) - fixed_features.keys())
        free_feature_bounds = bounds[:, free_feature_dims]  # (2, d' <= d)
    else:
        free_feature_bounds = bounds

    if linear_constraints is None:
        inequality_constraints = None
    else:
        A, b = linear_constraints
        inequality_constraints = []
        k, d = A.shape
        for i in range(k):
            indicies = A[i, :].nonzero(as_tuple=False).squeeze()
            coefficients = -A[i, indicies]
            rhs = -b[i, 0]
            inequality_constraints.append((indicies, coefficients, rhs))

    return optimize_acqf(
        acq_function=acq_function,
        bounds=free_feature_bounds,
        q=q,
        num_restarts=optimizer_options.get("num_restarts", 60),
        raw_samples=optimizer_options.get("raw_samples", 1024),
        options={
            "batch_limit": optimizer_options.get("batch_limit", 8),
            "maxiter": optimizer_options.get("maxiter", 200),
            "nonnegative": optimizer_options.get("nonnegative", False),
            "method": optimizer_options.get("method", "L-BFGS-B"),
        },
        inequality_constraints=inequality_constraints,
        fixed_features=None,  # handled inside the acquisition function
        post_processing_func=post_processing_func,
        batch_initial_conditions=batch_initial_conditions,
        return_best_only=True,
        sequential=sequential,
    )