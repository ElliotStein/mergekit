# Copyright (C) 2024 Charles O. Goddard
#
# This software is free software: you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This software is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program. If not, see http://www.gnu.org/licenses/.

from typing import Any, Dict, List, Optional

from mergekit.architecture import WeightInfo
from mergekit.common import ModelReference, ImmutableMap
from mergekit.graph import Task
from mergekit.io.tasks import GatherTensors, LoadTensor
from mergekit.metric_methods.base import MetricMethod
from mergekit.metric_methods.base import ConfigParameterDef

import torch
import torch.nn.functional as F

import numpy as np

def compute_histogram(tensor: torch.Tensor, n_bins: int) -> List[np.ndarray]:
    bin_counts, bin_edges = np.histogram(tensor.numpy(), bins=n_bins)
    bin_widths = np.diff(bin_edges)
    return bin_counts, bin_edges, bin_widths

def validate_tensors(tensors: List[torch.Tensor], weight_info: WeightInfo, expected_tensors: Optional[int] = 2):
    unique_shapes = set(t.shape for t in tensors)
    if len(unique_shapes) != 1:
        raise RuntimeError(f"Tensor size mismatch for {weight_info.name}, sizes: {list(unique_shapes)}")
    if expected_tensors:
        if len(tensors) != expected_tensors:
            raise RuntimeError(f"Expected {expected_tensors} tensors, got {len(tensors)}")
    
def SMAPE(
    tensors: List[torch.Tensor], **_kwargs
) -> Dict[str, Any]:
    numerator = torch.abs(tensors[0] - tensors[1])
    denominator = (torch.abs(tensors[0]) + torch.abs(tensors[1]))
    smape = torch.mean(torch.div(numerator, denominator), dim=1) 
    
    hist_info = compute_histogram(smape, 100)
    return {
        'SMAPE Histogram': {
            'count': hist_info[0],
            'edges': hist_info[1],
            'widths': hist_info[2]
        },
        'SMAPE_mean': smape.mean().item(),
        'SMAPE_std': smape.std().item()
    }

def cossim(
    tensors: List[torch.Tensor], **_kwargs
) -> torch.Tensor:
    cossim = F.cosine_similarity(tensors[0], tensors[1], dim=1)
    if _kwargs.get('angular_distance'):
        cossim = torch.acos(cossim.clamp(min=-1, max=1))/torch.pi 

    hist_info = compute_histogram(cossim, 100)
    return {
        'cossim Histogram': {
            'count': hist_info[0],
            'edges': hist_info[1],
            'widths': hist_info[2]
        },
        'cossim_mean': cossim.mean().item(),
        'cossim_std': cossim.std().item()
    }

def scale(
    tensors: List[torch.Tensor], **_kwargs
) -> torch.Tensor:

    norm_0 = torch.norm(tensors[0], dim=1)
    norm_1 = torch.norm(tensors[1], dim=1)

    scale_diff = torch.abs(norm_0 - norm_1) / ((norm_0 + norm_1) / 2)
        
    hist_info = compute_histogram(scale_diff, 100)
    return {
        'scale Histogram': {
            'count': hist_info[0],
            'edges': hist_info[1],
            'widths': hist_info[2]
        },
        'scale_mean': scale_diff.mean().item(),
        'scale_std': scale_diff.std().item()
    }

def mse(
    tensors: List[torch.Tensor], heatmap=False, **_kwargs
) -> torch.Tensor:
    
    res = {}

    if heatmap:
        num_heads = tensors[0].shape[0]
        heatmap = np.zeros((num_heads, num_heads))
        for i in range(num_heads):
            for j in range(num_heads):
                heatmap[i, j] = ((tensors[0][i] - tensors[1][j]) ** 2).mean().item()
        res['MSE Attn Heatmap'] = heatmap

    squared_diff = (tensors[0] - tensors[1]) ** 2
    mse_per_neuron = torch.mean(squared_diff, dim=1)

    hist_info = compute_histogram(mse_per_neuron, 100)
    res.update({
        'mse Histogram': {
            'count': hist_info[0],
            'edges': hist_info[1],
            'widths': hist_info[2]
        },
        'mse_mean': mse_per_neuron.mean().item(),
        'mse_std': mse_per_neuron.std().item()
    })
    return res

def ungroup_tensor(input_tensor, GQA_groups):
    """
    Ungroup a tensor by repeating its columns.

    Args:
        input_tensor (torch.Tensor): The input tensor to ungroup.
        GQA_groups (int): The number of GQA groups.

    Returns:
        torch.Tensor: The ungrouped tensor.
    """
    rows, cols = input_tensor.shape
    new_rows = rows * GQA_groups
    ungrouped_tensor = torch.zeros(new_rows, cols)

    for i in range(GQA_groups):
        ungrouped_tensor[i*rows:(i+1)*rows] = input_tensor[i].expand(rows, -1)
    
    return ungrouped_tensor

def restructure_tensor(input_tensor, num_rows):
    """
    Restructure a tensor by splitting its rows.

    Args:
        input_tensor (torch.Tensor): The input tensor to restructure.
        num_rows (int): The number of rows for splitting.

    Returns:
        torch.Tensor: The restructured tensor.
    """
    rows, cols = input_tensor.shape
    new_rows = rows // num_rows
    reshaped_tensor = input_tensor.view(new_rows, num_rows, cols)
    restructured_tensor = reshaped_tensor.permute(1, 0, 2)
    
    return restructured_tensor

def group_attn_head_weights(k_proj, q_proj, v_proj, o_proj, weight_info):

    num_heads = weight_info.num_heads
    GQA_groups = weight_info.GQA_groups

    k_proj = ungroup_tensor(k_proj, GQA_groups)
    v_proj = ungroup_tensor(v_proj, GQA_groups)

    k_proj = restructure_tensor(k_proj, num_heads)
    v_proj = restructure_tensor(v_proj, num_heads)
    q_proj = restructure_tensor(q_proj, num_heads)
    o_proj = restructure_tensor(o_proj.T, num_heads)

    return k_proj, v_proj, q_proj, o_proj

class AllMetricTask(Task[torch.Tensor]):
    gather_tensors: GatherTensors
    weight_info: WeightInfo
    angular_distance: bool

    def uses_accelerator(self) -> bool:
        return True

    def arguments(self) -> Dict[str, Task]:
        return {"tensors": self.gather_tensors}

    def execute(
        self, tensors: Dict[ModelReference, torch.Tensor], **_kwargs
    ) -> torch.Tensor:
        tensors = list(tensors.values())
        validate_tensors(tensors, self.weight_info, expected_tensors=2)
        res = {}
        if 'mlp' in self.weight_info.name:

            res.update(cossim(tensors, angular_distance=self.angular_distance))
            res.update(SMAPE(tensors))
            res.update(scale(tensors))
            res.update(mse(tensors))
            
        return res

    def group_label(self) -> Optional[str]:
        return self.gather_tensors.group_label()

class AttnTask(Task[torch.Tensor]):
    weights: Dict[str, GatherTensors]
    weight_infos: Dict[str, WeightInfo]
    weight_info: WeightInfo

    def uses_accelerator(self) -> bool:
        return True

    def arguments(self) -> Dict[str, Task]:

        return self.weights

    def execute(
        self, k_proj, v_proj, q_proj, o_proj, **_kwargs
    ) -> torch.Tensor:
        # Add metrics for attention weights
        res = {}
        models = list(q_proj.keys())

        k_proj_0, v_proj_0, q_proj_0, o_proj_0 = group_attn_head_weights(k_proj[models[0]], q_proj[models[0]], v_proj[models[0]], o_proj[models[0]], self.weight_info)
        k_proj_1, v_proj_1, q_proj_1, o_proj_1 = group_attn_head_weights(k_proj[models[1]], q_proj[models[1]], v_proj[models[1]], o_proj[models[1]], self.weight_info)
        
        # Metrics for K, V, Q, O projections

        model_0_heads = torch.cat([k_proj_0, v_proj_0, q_proj_0, o_proj_0], dim=1)
        model_1_heads = torch.cat([k_proj_1, v_proj_1, q_proj_1, o_proj_1], dim=1)
        
        # Metrics for heads
        res.update(mse([model_0_heads.view(model_0_heads.shape[0], -1), 
                        model_1_heads.view(model_1_heads.shape[0], -1)], 
                        heatmap=True))
        res.update(cossim([model_0_heads.view(model_0_heads.shape[0], -1), 
                           model_1_heads.view(model_1_heads.shape[0], -1)], 
                           angular_distance=True))
        res.update(scale([model_0_heads.view(model_0_heads.shape[0], -1),
                            model_1_heads.view(model_1_heads.shape[0], -1)]))

        return res

    def group_label(self) -> Optional[str]:
        # Use max of the group labels
        return max([gather_tensor.group_label() for gather_tensor in list(self.weights.values())]) # Check this (X)
    
    def __hash__(self):
        return hash(self.weight_info)

    def __eq__(self, other):
        if not isinstance(other, AttnTask):
            return False
        return self.weight_info == other.weight_info

class DummyTask(Task[torch.Tensor]):
    gather_tensors: GatherTensors
    weight_info: WeightInfo
    def uses_accelerator(self) -> bool:
        return True

    def arguments(self) -> Dict[str, Task]:
        return {"tensors": self.gather_tensors}

    def execute(
        self, **_kwargs
    ) -> torch.Tensor:
        
        return 
    
    def group_label(self) -> Optional[str]:
        return self.gather_tensors.group_label()
        

class AllMetric(MetricMethod):
    attn_weight_tensors: Optional[list] = []
    attn_weight_infos: Optional[list] = []

    attn_weight_dict: Optional[Dict[str, torch.Tensor]] = {}
    attn_info_dict: Optional[Dict[str, WeightInfo]] = {}


    attn_parts: Optional[List[str]] = ['k_proj', 'v_proj', 'q_proj', 'o_proj']
    block_count: Optional[int] = 0
    def parameters(self) -> List[ConfigParameterDef]:
        return [
            ConfigParameterDef(name="angular_distance", required=False, default_value=False),
        ]
    def make_task(
        self,
        *,
        output_weight: WeightInfo,
        parameters: Optional[Dict[str, Any]] = None,
        tensors: GatherTensors,
        **_kwargs,
    ) -> Task:
        
        if 'self_attn' in output_weight.name:
            # collect all attention weights
            for part in self.attn_parts: # also check only one key
                if part in output_weight.name:
                   self.attn_weight_dict[part] = tensors
                   self.attn_info_dict[part] = output_weight   

            # if all attention weights are collected, create attention task
            if set(list(self.attn_weight_dict.keys())) == set(self.attn_parts):
                weights, infos = self.attn_weight_dict, self.attn_info_dict
                self.attn_weight_dict, self.attn_info_dict = {}, {}
                weight_info = WeightInfo(
                    name=f"Attention Block {self.block_count}",
                    force_dtype=None,
                    optional=False,
                    aliases=None,
                    GQA_groups=4,
                    num_heads=32
                )
                self.block_count += 1
                return AttnTask(weights=weights, weight_infos=infos, weight_info=weight_info)
        if 'mlp' in output_weight.name:        
            return AllMetricTask(
                gather_tensors=tensors,
                weight_info=output_weight,
                angular_distance=parameters["angular_distance"]
            )
        else:
            return DummyTask(gather_tensors=tensors, weight_info=output_weight)


