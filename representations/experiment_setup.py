from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Any, Optional

import numpy as np
import torch
from tqdm import tqdm
import h5py

from mergekit.architecture import WeightInfo
from mergekit.metric_methods.base import Results, Layer
from mergekit.metric_methods.aggregator_metrics import ModelAnalysisType, LayerComparisonType, METRICS_TABLE, MetricAggregator

def check_memory(h5file):
    # Check if full data can be loaded into memory
    return True

class LayerByIndex:
    def __init__(self, reps_path: str, load_into_memory: bool = True):
        self.reps_path = reps_path
        self.representations = None
        self.layers = None
        self.load_into_memory = load_into_memory
        self.in_memory_data = None
        self.device = 'cuda' if torch.cuda.is_available() else 'cpu'

    def __enter__(self):
        self.representations = h5py.File(self.reps_path, 'r')
        self.layers = list(self.representations.keys())
        
        if self.load_into_memory:
            print("Loading representations into memory")
            self.in_memory_data = {layer: {} for layer in self.layers}
            for layer_name in tqdm(self.layers, leave=False, initial = 1, desc='Loading Layer', total=len(self.layers)):
                for batch_name, batch_data in self.representations[layer_name].items():
                    data = torch.tensor(batch_data[...]).to(self.device) 
                    self.in_memory_data[layer_name][batch_name] = data
        return self

    def __exit__(self, *args, **kwargs):
        if self.representations:
            self.representations.close()
        self.in_memory_data = None

    def __getitem__(self, idx: int):
        if self.load_into_memory:
            return self.in_memory_data[self.layers[idx]]
        else:
            return self.representations[self.layers[idx]] #(X)

    def __len__(self) -> int:
        return len(self.layers)

    # def __iter__(self):
    #     if self.load_into_memory:
    #         return ((layer, self.in_memory_data[layer]) for layer in self.layers)
    #     else:
    #         return ((layer, self.representations[layer]) for layer in self.layers)
    def __iter__(self):
        if self.load_into_memory:
            return ((layer, self.in_memory_data[layer]) for layer in self.layers)
        else:
            return ((layer, self.representations[layer]) for layer in self.layers)

def valid_experiment(analysis_type, comparison_type):
    if comparison_type == LayerComparisonType.ALL:
        raise ValueError("Comparison type 'all_layers' is not supported")
    if analysis_type == ModelAnalysisType.COMPARISON and comparison_type in [LayerComparisonType.BLOCK, LayerComparisonType.SINGLE]:
        raise ValueError("Comparison type 'single' and 'block' only supported for individual analysis")
    if analysis_type == ModelAnalysisType.INDIVIDUAL and comparison_type == LayerComparisonType.CORRESPONDING:
        raise ValueError("Comparison type 'corresponding' only supported for comparison analysis")

# Experiment Loops
def single(representations: LayerByIndex, metric_classes: List[MetricAggregator], results: Optional[Results], device='cpu'):
    if not results:
        results = Results()
    for layer_name, layer in tqdm(representations, desc='Analysing Layer', 
                                    total=len(representations), leave=False, initial = 1):
        # layer_name = layer.name.split('/')[-1] # layer is a dictionary of batches, doens't have .name attribute. 
        # num = f"{int(layer_name.split('_')[-1]):03d}"
        # layer_name = f"Layer {num}"
        metrics = [metric_class(device=device) for metric_class in metric_classes]

        for batch in tqdm(layer.values(), desc='Batch', 
                                    total=len(layer), leave=False, initial = 1):
            # batch = torch.tensor(layer[batch][:], device=device) # Redundant now as LayerByIndex is already returning torch tensors
            
            # Calculate the metrics for each batch
            for metric in metrics:
                metric.process_batch(batch)

        layer_results = Layer(WeightInfo(name=layer_name))
        # Aggregate over the batches and add to the layer results
        for metric in metrics:
            layer_results.add_metric(metric.aggregate(), metric.__class__.__name__.lower())
            # metric.clear()
        
        results.add_layer(layer_results, layer_name)

    return results

def corresponding(representations_0: LayerByIndex, representations_1: LayerByIndex, metric_classes: List[MetricAggregator], results: Optional[Results], device='cpu'):
    if not results:
            results = Results()

    for (layer_0_name, layer_0), (layer_1_name, layer_1) in tqdm(zip(representations_0, representations_1), 
                                desc='Comparing Representations at layer', 
                                total=len(representations_0), initial = 1):
        
        # layer_0_name = layer_0.name.split('/')[-1]
        # layer_1_name = layer_1.name.split('/')[-1]
        if layer_0_name != layer_1_name:
            raise ValueError(f'Layer mismatch: {layer_0_name} != {layer_1_name}')
        num = f"{int(layer_0_name.split('_')[-1]):03d}"
        layer_name = f"Layer {num}"

        metrics = [metric_class(device=device) for metric_class in metric_classes]

        for batch_0, batch_1 in tqdm(zip(layer_0.values(), layer_1.values()), 
                                    desc='Batch', total=len(layer_0), leave=False, initial = 1):
            # batch_0 = torch.tensor(layer_0[batch_0][:], device=device)
            # batch_1 = torch.tensor(layer_1[batch_1][:], device=device)
            
            # Calculate the metrics for each batch
            for metric in metrics:
                metric.process_batch(batch_0, batch_1)

        layer_results = Layer(WeightInfo(name=layer_name))
        # Aggregate over the batches and add to the layer results
        for metric in metrics:
            layer_results.add_metric(metric.aggregate(), metric.__class__.__name__.lower())
            # metric.clear()

        results.add_layer(layer_results, layer_name)
    
    return results

def block(representations, block_size, metric_classes, device='cpu'):
    out = {metric().__class__.__name__.lower(): [] for metric in metric_classes}
    for idx, (block_start_name, block_start) in tqdm(enumerate(representations), desc=f'Comparing {block_size}-block, Block Start at Layer', 
                                     total=len(representations) - block_size, leave=False, initial = 1):
            if idx + block_size >= len(representations):
                break

            # Create metrics
            metrics = [metric_class(device=device) for metric_class in metric_classes]
            block_end = representations[idx + block_size]

            for batch_0, batch_1 in tqdm(zip(block_start.values(), block_end.values()), desc='Batch', 
                                         total=len(block_start), leave=False, initial = 1):
                # batch_0 = torch.tensor(block_start[batch_0][:]).to(device)
                # batch_1 = torch.tensor(block_end[batch_1][:]).to(device)
                
                for metric in metrics:
                    metric.process_batch(batch_0, batch_1)
            
            # Aggregate metrics and add to results
            for metric in metrics:
                out[metric.__class__.__name__.lower()].append(metric.aggregate())

    for metric in out:
        out[metric] = np.array(out[metric])
    return out

def all_layers(representations_0, representations_1, metric_classes, results, device='cpu'):
    for layer_0_name, layer_0 in tqdm(representations_0, desc='Model 0 Layers', 
                                     total=len(representations_0), leave=False, initial = 1):
        for layer_1_name, layer_1 in tqdm(representations_1, desc='Model 1 Layers', 
                                    total=len(representations_1), leave=False, initial = 1):
            if len(layer_0) != len(layer_1):
                raise ValueError(f'Layer mismatch: {len(layer_0)} != {len(layer_1)}')

            metrics = [metric_class(device=device) for metric_class in metric_classes]

            for batch_0, batch_1 in tqdm(zip(layer_0.values(), layer_1.values()), desc='Batch', 
                                        total=len(layer_0), leave=False, initial = 1):
                # batch_0 = torch.tensor(layer_0[batch_0][:]).to(device)
                # batch_1 = torch.tensor(layer_1[batch_1][:]).to(device)
                
                for metric in metrics:
                    metric.process_batch(batch_0, batch_1)
            
            layer_results = Layer(WeightInfo(name=f"{layer_0_name} - {layer_1_name}"))
            for metric in metrics:
                layer_results.add_metric(metric.aggregate(), metric.__class__.__name__.lower())

            results.add_layer(layer_results, f"{layer_0_name} - {layer_1_name}")
    
    return results

@dataclass
class Configuration:
    representation_paths: List[Path]
    metrics: Dict[str, bool]
    analysis_type: ModelAnalysisType
    comparison_type: LayerComparisonType
    out_dir: Path
    device: str
    data: Optional[LayerByIndex] = None

    @classmethod
    def from_dict(cls, config_dict: Dict):
        return cls(
            representation_paths=list(config_dict['stored_representations'].iterdir()),
            metrics=config_dict['metrics'],
            analysis_type=ModelAnalysisType(config_dict['analysis_type']),
            comparison_type=LayerComparisonType(config_dict['comparison_type']),
            out_dir=Path(config_dict['out_dir']),
            device='cuda' if torch.cuda.is_available() else 'mps' if torch.backends.mps.is_available() else 'cpu'
        ).validate()

    def validate(self):
        if self.comparison_type == LayerComparisonType.ALL:
            raise ValueError("Comparison type 'all_layers' is not supported")
        if self.analysis_type == ModelAnalysisType.COMPARISON and self.comparison_type in [LayerComparisonType.BLOCK, LayerComparisonType.SINGLE]:
            raise ValueError("Comparison type 'single' and 'block' only supported for individual analysis")
        if self.analysis_type == ModelAnalysisType.INDIVIDUAL and self.comparison_type == LayerComparisonType.CORRESPONDING:
            raise ValueError("Comparison type 'corresponding' only supported for comparison analysis")
        return self
        
class Experiment(ABC):
    @abstractmethod
    def run(self, config: Configuration):
        pass

class SingleExperiment(Experiment):
    def run(self, config: Configuration):
        # Implementation for single experiment
        use_metrics = {name: METRICS_TABLE[name] 
                   for name, enabled in config.metrics.items() 
                   if enabled}

        metrics = [metric for metric in use_metrics.values() if metric().valid_for[LayerComparisonType.SINGLE.value]]
        for representation_path in config.representation_paths:
            individual_results = Results()
            individual_results.model_paths = [representation_path]

            if not representation_path.exists():
                raise FileNotFoundError(f"Representation file {representation_path} not found")

            with LayerByIndex(representation_path, load_into_memory = check_memory(representation_path)) as representations:
                individual_results = single(representations, 
                                            metrics, 
                                            results=individual_results, 
                                            device=config.device)
            
            out_path = config.out_dir / f"{str(representation_path).split('/')[-1].split('.')[0]}+{config.analysis_type}+{config.comparison_type}.json"
            individual_results.save(out_path)

class CorrespondingExperiment(Experiment):
    def run(self, config: Configuration):
        use_metrics = {name: METRICS_TABLE[name] 
                   for name, enabled in config.metrics.items() 
                   if enabled}

        metrics = [metric for metric in use_metrics.values() if metric().valid_for[LayerComparisonType.CORRESPONDING.value]]
        comparison_results = Results()
        stop = False
        for rep_0 in config.representation_paths:
            for rep_1 in config.representation_paths:
                if ((rep_0 == rep_1 and config.analysis_type == ModelAnalysisType.INDIVIDUAL) or \
                    (rep_0 != rep_1 and config.analysis_type == ModelAnalysisType.COMPARISON) and not stop):
                    if rep_0 != rep_1:
                        stop = True

                    with LayerByIndex(rep_0) as representations_0, LayerByIndex(rep_1) as representations_1:    
                        comparison_results = corresponding(representations_0=representations_0,
                                                            representations_1=representations_1,
                                                            metric_classes=metrics,
                                                            results=comparison_results,
                                                            device=config.device)
                        comparison_results.model_paths = [rep_0, rep_1] if rep_0 != rep_1 else [rep_0]
                    
                    out_path = config.out_dir / f"{str([str(rep).split('/')[-1].split('.')[0] for rep in [rep_0, rep_1]])}+{config.analysis_type}+{config.comparison_type}.json"
                    comparison_results.save(out_path)

class BlockExperiment(Experiment):
    def run(self, config: Configuration):
        use_metrics = {name: METRICS_TABLE[name] 
                   for name, enabled in config.metrics.items() 
                   if enabled}

        metrics = [metric for metric in use_metrics.values() if metric().valid_for[LayerComparisonType.BLOCK.value]]
        heatmaps = {}
        for representation_path in config.representation_paths:
            block_results = Results()
            block_results.model_paths = [representation_path]
            if not representation_path.exists():
                raise FileNotFoundError(f"Representation file {representation_path} not found")
            for metric in metrics:
                heatmaps[metric().__class__.__name__.lower()] = np.array([])
            with LayerByIndex(representation_path, load_into_memory = check_memory(representation_path)) as representations:
                for block_size in range(1, 9):
                    block_res = block(representations=representations, 
                                        block_size=block_size, 
                                        metric_classes=metrics, 
                                        device=config.device)
                    for metric in metrics:
                        heatmaps[metric().__class__.__name__.lower()] = np.append(heatmaps[metric().__class__.__name__.lower()], block_res[metric().__class__.__name__.lower()])
            
            for metric in metrics:
                block_results.across_layer_metrics[metric().__class__.__name__.lower()] = heatmaps[metric().__class__.__name__.lower()] # Definitely a simpler way to code this (X)

            out_path = config.out_dir / f"{str(representation_path).split('/')[-1].split('.')[0]}+{config.analysis_type}+{config.comparison_type}.json"
            block_results.save(out_path)

class AllLayersExperiment(Experiment):
    def run(self, config: Configuration):
        use_metrics = {name: METRICS_TABLE[name] 
                   for name, enabled in config.metrics.items() 
                   if enabled}

        metrics = [metric for metric in use_metrics.values() if metric().valid_for[LayerComparisonType.ALL.value]]

        stop = False
        for rep_0 in config.representation_paths:
            for rep_1 in config.representation_paths:
                if ((rep_0 == rep_1 and config.analysis_type == ModelAnalysisType.INDIVIDUAL) or \
                    (rep_0 != rep_1 and config.analysis_type == ModelAnalysisType.COMPARISON) and not stop):
                    if rep_0 != rep_1:
                        stop = True
                    with LayerByIndex(rep_0) as representations_0, LayerByIndex(rep_1) as representations_1:

                        comparison_results = all_layers(representations_0=representations_0,
                                                representations_1=representations_1,
                                                metric_classes=metrics,
                                                results=comparison_results,
                                                device=config.device)
                        comparison_results.model_paths = [rep_0, rep_1]
        
                        out_path = config.out_dir / f"{str([str(rep).split('/')[-1].split('.')[0] for rep in config.representation_paths])}+{config.analysis_type}+{config.comparison_type}.json"
                        comparison_results.save(out_path)
      

class ExperimentFactory:
    experiments: Dict[str, Experiment] = {
        "single": SingleExperiment,
        "corresponding": CorrespondingExperiment,
        "block": BlockExperiment,
        "all_layers": AllLayersExperiment,
    }

    @classmethod
    def create(cls, experiment_type: str) -> Experiment:
        experiment_class = cls.experiments.get(experiment_type)
        if not experiment_class:
            raise ValueError(f"Unknown experiment type: {experiment_type}")
        return experiment_class()
