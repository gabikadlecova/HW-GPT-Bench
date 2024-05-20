import logging
from argparse import ArgumentParser
from pathlib import Path

from syne_tune.backend import LocalBackend
from syne_tune.optimizer.baselines import (
    RandomSearch,
    GridSearch,
    MOREA,
    NSGA2,
    MORandomScalarizationBayesOpt,
    MOASHA,
    EHVI,
)
from syne_tune import Tuner, StoppingCriterion
from syne_tune.config_space import randint, uniform, loguniform, choice
from baselines.local_search import LS
from syne_tune.optimizer.schedulers.multiobjective.linear_scalarizer import (
    LinearScalarizedScheduler,
)
import os
import json
import pickle

# Configuration space (or search space)


if __name__ == "__main__":
    logging.getLogger().setLevel(logging.DEBUG)
    # [1]
    parser = ArgumentParser()
    parser.add_argument(
        "--method",
        type=str,
        choices=(
            "RS",
            "BO",
            "Grid",
            "MOREA",
            "LS",
            "NSGA2",
            "LSBO",
            "RSBO",
            "MOASHA",
            "EHVI",
        ),
        default="RS",
    )
    parser.add_argument(
        "--random_seed",
        type=int,
        default=31415927,
    )
    parser.add_argument(
        "--n_workers",
        type=int,
        default=1,
    )
    parser.add_argument(
        "--max_wallclock_time",
        type=int,
        default=200,
    )
    parser.add_argument(
        "--experiment_tag",
        type=str,
        default="mogpt",
    )
    parser.add_argument(
        "--max_num_layers",
        type=int,
        default=12,
    )
    parser.add_argument(
        "--device_latency",
        type=str,
        default="rtx2080",
    )
    parser.add_argument(
        "--device_energy",
        type=str,
        default="a6000",
    )
    args, _ = parser.parse_known_args()
    config_space = {
        "max_num_layers": args.max_num_layers,
        "device_latency": args.device_latency,
        "device_energy": args.device_energy,
        "num_layers": choice([10, 11, 12]),
        "embed_dim": choice([768, 384, 192]),
        "bias": choice([True, False]),
    }
    for i in range(args.max_num_layers):
        config_space[f"mlp_ratio_{i}"] = choice([2, 3, 4])
        config_space[f"num_heads_{i}"] = choice([4, 8, 12])

    # Here, we specify the training script we want to tune
    # - `mode` and `metric` must match what is reported in the training script
    # - Metrics need to be reported after each epoch, `resource_attr` must match
    #   what is reported in the training script
    train_file = "run_nas_lm_from_scratch_3_objs.py"
    entry_point = Path(__file__).parent / train_file
    max_resource_level = 1  # Maximum number of training epochs
    mode = "min"
    metrics = ["perplexity", "energy", "latency"]
    resource_attr = "epoch"
    max_resource_attr = "epochs"

    # Additional fixed parameters  [2]
    config_space.update(
        {
            max_resource_attr: max_resource_level,
            "dataset_path": "./",
        }
    )

    # Local backend: Responsible for scheduling trials  [3]
    # The local backend runs trials as sub-processes on a single instance
    trial_backend = LocalBackend(entry_point=str(entry_point))

    # Scheduler: Depends on `args.method`  [4]
    scheduler = None
    # Common scheduler kwargs
    method_kwargs_single = dict(
        metric=metrics[0],
        mode=mode,
        random_seed=args.random_seed,
        max_resource_attr=max_resource_attr,
        search_options={"num_init_random": 8},
    )
    method_kwargs_multi = dict(
        metric=metrics,
        mode=["min", "min", "min"],
        random_seed=args.random_seed,
        max_resource_attr=max_resource_attr,
        search_options={"num_init_random": 8},
    )
    method_kwargs_moasha = dict(metrics=metrics, mode=["min", "min", "min"])
    sch_type = "promotion" if args.method.endswith("PROM") else "stopping"
    if args.method == "RS":
        scheduler = RandomSearch(config_space, **method_kwargs_single)
    elif args.method == "Grid":
        print(method_kwargs_single)
        scheduler = GridSearch(config_space, **method_kwargs_single)
    elif args.method == "MOREA":
        print(method_kwargs_multi)
        scheduler = MOREA(config_space, **method_kwargs_multi)
    elif args.method == "LS":
        scheduler = LS(config_space, **method_kwargs_multi)
    elif args.method == "NSGA2":
        scheduler = NSGA2(config_space, **method_kwargs_multi)
    elif args.method == "LSBO":
        scheduler = LinearScalarizedScheduler(
            config_space, searcher="bayesopt", **method_kwargs_multi
        )
    elif args.method == "RSBO":
        scheduler = MORandomScalarizationBayesOpt(config_space, **method_kwargs_multi)
    elif args.method == "MOASHA":
        scheduler = MOASHA(
            config_space,
            time_attr="st_worker_time",
            grace_period=1,
            max_t=5,
            reduction_factor=3,
            **method_kwargs_moasha,
        )
    elif args.method == "EHVI":
        scheduler = EHVI(config_space, **method_kwargs_multi)
    else:
        raise NotImplementedError(args.method)

    # Stopping criterion: We stop after `args.max_wallclock_time` seconds
    # [5]
    stop_criterion = StoppingCriterion(max_num_evaluations=3000)

    tuner = Tuner(
        trial_backend=trial_backend,
        scheduler=scheduler,
        stop_criterion=stop_criterion,
        n_workers=args.n_workers,
        tuner_name=args.experiment_tag,
        metadata={
            "seed": args.random_seed,
            "algorithm": args.method,
            "tag": args.experiment_tag,
        },
    )

    tuner.run()
    from syne_tune.experiments import load_experiment

    print(tuner.name)
    df = load_experiment(tuner.name).results
    configs = []
    runtime_traj = []
    energy = []
    latency = []
    perplexity = []
    print(df.head())
    for trial, trial_df in df.groupby("trial_id"):
        idx = trial_df["perplexity"].idxmin()
        runtime_traj.append(float(trial_df.st_tuner_time.iloc[-1]))
        perplexity.append(trial_df["perplexity"].values)
        energy.append(trial_df["energy"].values)
        latency.append(trial_df["latency"].values)
        config = {}
        for hyper in config_space.keys():
            c = trial_df.iloc[0]["config_" + hyper]
            config[hyper] = c
        configs.append(config)
        print(configs)
    results = {
        "configs": configs,
        "runtime_traj": runtime_traj,
        "perplexity": perplexity,
        "energy": energy,
        "latency": latency,
    }

    os.makedirs("results", exist_ok=True)
    save_path = (
        f"results/"
        + args.experiment_tag
        + "_"
        + args.method
        + "_"
        + args.device_latency
        + "_"
        + args.device_energy
        + ".pickle"
    )
    with open(save_path, "wb") as f:
        pickle.dump(results, f)