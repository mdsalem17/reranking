"""Both dense and sparse information retrieval is done via HF-Datasets, using FAISS and ElasticSearch, respectively

Usage:
hp.py <type> <dataset> <config> [--k=<k> --disable_caching --cleanup_cache_files --metrics=<path> --test=<dataset>]

Options:
--k=<k>                 Hyperparameter to search for the k nearest neighbors [default: 100].
--disable_caching       Disables Dataset caching (useless when using save_to_disk), see datasets.set_caching_enabled()
--cleanup_cache_files   Clean up all cache files in the dataset cache directory, 
                        excepted the currently used one, see Dataset.cleanup_cache_files()
                        Useful to avoid saturating disk storage (caches are only deleted when exiting with --disable_caching)
--metrics=<path>        Path to save the metrics in JSON and TeX format (only applicable with --test)
--test=<dataset>        Name of the test dataset
"""
import warnings

from docopt import docopt
import json
from collections import Counter
import time
from pathlib import Path
from copy import deepcopy

import numpy as np
from datasets import load_from_disk, set_caching_enabled
import ranx

import optuna

from meerqat.ir.metrics import find_relevant_batch
from meerqat.ir.search import Searcher, format_qrels_indices


class Objective:
    """Callable objective compatible with optuna."""
    def __init__(self, dataset, do_cache_relevant, metric_for_best_model=None, eval_dataset=None,
                 map_kwargs={}, fn_kwargs={}, cleanup_cache_files=False, **kwargs):
        self.dataset = dataset
        self.searcher = Searcher(**kwargs)
        # HACK: sleep until elasticsearch is good to go
        time.sleep(60)
        if metric_for_best_model is None:
            self.metric_for_best_model = f"mrr@{self.searcher.k}"
        else:
            self.metric_for_best_model = metric_for_best_model
        self.eval_dataset = eval_dataset
        self.map_kwargs = map_kwargs
        self.fn_kwargs = fn_kwargs
        self.do_cache_relevant = do_cache_relevant
        self.cleanup_cache_files = cleanup_cache_files

    def __call__(self, trial):
        pass

    def evaluate(self, best_params):
        """
        Should evaluate self.eval_dataset with best_params

        Parameters
        ----------
        best_params: dict

        Returns
        -------
        report: ranx.Report
        """
        pass

    def cache_relevant(self, batch, do_copy=False):
        """
        Caches relevant passages w.r.t. union of all search results.
        """
        all_indices = self.searcher.union_results(batch)
        relevant_batch = deepcopy(batch['provenance_indices']) if do_copy else batch['provenance_indices']
        provenance_indices = find_relevant_batch(all_indices, batch['output'], self.searcher.reference_kb,
                                                 reference_key=self.searcher.reference_key, relevant_batch=relevant_batch)
        str_indices_batch, non_empty_scores = format_qrels_indices(provenance_indices)
        self.searcher.qrels.add_multi(
            q_ids=batch['id'],
            doc_ids=str_indices_batch,
            scores=non_empty_scores
        )
        return batch
    
    def cache_relevant_dataset(self, do_copy=False):
        self.dataset.map(self.cache_relevant, batched=True, fn_kwargs=dict(do_copy=do_copy), **self.map_kwargs)
        self.keep_reference_kb = self.searcher.reference_kb
        # so that subsequent calls to searcher.fuse_and_compute_metrics will not call find_relevant_batch
        self.searcher.reference_kb = None


class FusionObjective(Objective):
    def __init__(self, *args, hyp_hyp=None, **kwargs):
        super().__init__(*args, **kwargs)

        fusion_method = self.searcher.fusion_method

        # default parameters
        if hyp_hyp is None:
            if fusion_method == 'interpolation':
                hyp_hyp = {}
                default = {
                    "bounds": (0, 1.1),
                    "step": 0.1
                }
                for kb in self.searcher.kbs.values():
                    for index_name in kb.indexes.keys():
                        hyp_hyp[f"{index_name}.interpolation_weight"] = default
            else:
                raise NotImplementedError()

        self.hyp_hyp = hyp_hyp

    def __call__(self, trial):
        fusion_method = self.searcher.fusion_method
        if fusion_method == 'interpolation':
            interpolation_weight_sum = 0
            for kb in self.searcher.kbs.values():
                for index_name, index in kb.indexes.items():
                    hp_name = f"{index_name}.interpolation_weight"
                    hyp_hyp = self.hyp_hyp[hp_name]["bounds"]
                    index.interpolation_weight = trial.suggest_float(hp_name, *hyp_hyp)
                    interpolation_weight_sum += index.interpolation_weight
            # constrain all weights to sum to 1, do not compute trial otherwise
            if abs(1 - interpolation_weight_sum) > 1e-6:
                raise optuna.TrialPruned
        else:
            raise NotImplementedError()

        self.dataset.map(self.searcher.fuse_and_compute_metrics, fn_kwargs=self.fn_kwargs, batched=True, **self.map_kwargs)
        if self.cleanup_cache_files:
            self.dataset.cleanup_cache_files()
        metric = ranx.evaluate(self.searcher.qrels, self.searcher.runs["fusion"], self.metric_for_best_model)
        return metric

    def evaluate(self, best_params):
        # reset to erase qrels and runs of the validation set
        self.searcher.qrels = ranx.Qrels()
        run = ranx.Run()
        run.name = "fusion"
        self.searcher.runs = dict(fusion=run)
        # fill qrels
        self.eval_dataset.map(self.cache_relevant, batched=True, fn_kwargs=dict(do_copy=True), **self.map_kwargs)

        fusion_method = self.searcher.fusion_method
        if fusion_method == 'interpolation':
            for kb in self.searcher.kbs.values():
                for index_name, index in kb.indexes.items():
                    index.interpolation_weight = best_params[f"{index_name}.interpolation_weight"]
        else:
            raise NotImplementedError()

        self.eval_dataset = self.eval_dataset.map(self.searcher.fuse_and_compute_metrics, fn_kwargs=self.fn_kwargs, batched=True, **self.map_kwargs)
        report = ranx.compare(
            self.searcher.qrels,
            runs=self.searcher.runs.values(),
            **self.searcher.metrics_kwargs
        )
        return report


class BM25Objective(Objective):
    def __init__(self, *args, hyp_hyp=None, settings=None, **kwargs):                 
        super().__init__(*args, **kwargs)
        # default parameters
        if hyp_hyp is None:
            self.hyp_hyp = {
                "b": {
                    "bounds": (0, 1),
                    "step": 0.1
                },
                "k1": {
                    "bounds": (0, 3),
                    "step": 0.1
                }
            }
        else:
            self.hyp_hyp = hyp_hyp
        if settings is None:
            self.settings = {'similarity': {'karpukhin': {'b': 0.75, 'k1': 1.2}}}
        else:
            self.settings = settings

        # check that there is a single ES index + save ES client and ES client’s name
        self.index_name = None
        for kb in self.searcher.kbs.values():
            for index_name, index in kb.indexes.items():
                if index.es:
                    assert self.index_name is None, f"Expected a single ES index, got {self.index_name} and {index_name}"
                    self.index_name = index_name
                    self.es_client = kb.es_client
                    es_index = kb.dataset._indexes[self.index_name]
                    self.es_index_name = es_index.es_index_name

        assert self.index_name is not None, "Did not find an ES index"

    def __call__(self, trial):
        settings = self.settings

        # suggest hyperparameters
        b = trial.suggest_float("b", *self.hyp_hyp["b"]["bounds"])
        k1 = trial.suggest_float("k1", *self.hyp_hyp["k1"]["bounds"])
        for parameters in settings['similarity'].values():
            parameters['b'] = b
            parameters['k1'] = k1
        # close index, update its settings then open it
        self.es_client.indices.close(self.es_index_name)
        self.es_client.indices.put_settings(settings, self.es_index_name)
        self.es_client.indices.open(self.es_index_name)

        self.dataset.map(self.searcher, fn_kwargs=self.fn_kwargs, batched=True, **self.map_kwargs)
        if self.searcher.do_fusion:
            run = self.searcher.runs["fusion"]
        else:           
            run = self.searcher.runs[self.index_name]
        metric = ranx.evaluate(self.searcher.qrels, run, self.metric_for_best_model)
        return metric

    def evaluate(self, best_params):
        # reset to erase qrels and runs of the validation set
        self.searcher.qrels = ranx.Qrels()
        self.searcher.runs = dict()
        for kb in self.searcher.kbs.values():
            for index_name, index in kb.indexes.items():
                run = ranx.Run()
                run.name = index_name
                self.searcher.runs[index_name] = run
        if self.searcher.do_fusion:
            run = ranx.Run()
            run.name = "fusion"
            self.searcher.runs["fusion"] = run
        
        settings = self.settings

        for parameters in settings['similarity'].values():
            parameters.update(best_params)
        # close index, update its settings then open it
        self.es_client.indices.close(self.es_index_name)
        self.es_client.indices.put_settings(settings, self.es_index_name)
        self.es_client.indices.open(self.es_index_name)

        self.eval_dataset = self.eval_dataset.map(self.searcher, fn_kwargs=self.fn_kwargs, batched=True, **self.map_kwargs)
        report = ranx.compare(
            self.searcher.qrels,
            runs=self.searcher.runs.values(),
            **self.searcher.metrics_kwargs
        )
        return report


def get_objective(objective_type, train_dataset, **objective_kwargs):
    if objective_type == 'fusion':
        objective = FusionObjective(train_dataset, do_cache_relevant=True, **objective_kwargs)
        if objective.searcher.fusion_method == 'interpolation':
            search_space = {}
            for kb in objective.searcher.kbs.values():
                for index_name in kb.indexes.keys():
                    hp_name = f"{index_name}.interpolation_weight"
                    hyp_hyp = objective.hyp_hyp[hp_name]
                    search_space[hp_name] = np.arange(*hyp_hyp["bounds"], hyp_hyp["step"]).tolist()
            default_study_kwargs = dict(direction='maximize', sampler=optuna.samplers.GridSampler(search_space))
        else:
            default_study_kwargs = {}
    elif objective_type == 'bm25':
        objective = BM25Objective(train_dataset, do_cache_relevant=False, **objective_kwargs)
        hyp_hyp = objective.hyp_hyp
        search_space = dict(b=np.arange(*hyp_hyp['b']["bounds"], hyp_hyp['b']["step"]).tolist(),
                            k1=np.arange(*hyp_hyp['k1']["bounds"], hyp_hyp['k1']["step"]).tolist())
        default_study_kwargs = dict(direction='maximize', sampler=optuna.samplers.GridSampler(search_space))
    else:
        raise ValueError(f"Invalid objective type: {objective_type}")
    return objective, default_study_kwargs


def hyperparameter_search(study_name=None, storage=None, metric_save_path=None,
                          optimize_kwargs={}, study_kwargs={}, cleanup_cache_files=False, **objective_kwargs):
    objective, default_study_kwargs = get_objective(cleanup_cache_files=cleanup_cache_files, **objective_kwargs)
    default_study_kwargs.update(study_kwargs)
    if storage is None and study_name is not None:
        storage = f"sqlite:///{study_name}.db"
    study = optuna.create_study(storage=storage, study_name=study_name, load_if_exists=True, **default_study_kwargs)
    if objective.do_cache_relevant:
        objective.cache_relevant_dataset()
    # actual optimisation
    study.optimize(objective, **optimize_kwargs)
    print(f"Best value: {study.best_value} ({objective.metric_for_best_model})")
    print(f"Best hyperparameters: {study.best_params}")

    # apply hyperparameters on test set
    if eval_dataset is not None:
        if objective.do_cache_relevant:
            objective.searcher.reference_kb = objective.keep_reference_kb
        report = objective.evaluate(study.best_params)
        print(report)

        if metric_save_path is not None:
            metric_save_path = Path(metric_save_path)
            metric_save_path.mkdir(exist_ok=True)
            # N. B. qrels and runs are overwritten in Searcher every time there's a call to add_multi
            objective.searcher.qrels.save(metric_save_path / "qrels.trec")
            report.save(metric_save_path / "metrics.json")
            with open(metric_save_path / "metrics.tex", 'wt') as file:
                file.write(report.to_latex())
            for index_name, run in objective.searcher.runs.items():
                run.save(metric_save_path / f"{index_name}.trec")

    return objective.eval_dataset

if __name__ == '__main__':
    args = docopt(__doc__)
    dataset_path = args['<dataset>']
    dataset = load_from_disk(dataset_path)
    set_caching_enabled(not args['--disable_caching'])
    cleanup_cache_files = args['--cleanup_cache_files']
    config_path = args['<config>']
    with open(config_path, 'r') as file:
        config = json.load(file)
    format_kwargs = config.pop('format', {})
    dataset.set_format(**format_kwargs)

    k = int(args['--k'])

    eval_dataset_path = args['--test']
    if eval_dataset_path:
        eval_dataset = load_from_disk(eval_dataset_path)
        eval_dataset.set_format(**format_kwargs)
    else:
        eval_dataset = None
    eval_dataset = hyperparameter_search(objective_type=args['<type>'], train_dataset=dataset, k=k,
                                         metric_save_path=args['--metrics'], eval_dataset=eval_dataset, 
                                         cleanup_cache_files=cleanup_cache_files, **config)
    if eval_dataset is not None:
        eval_dataset.save_to_disk(eval_dataset_path)