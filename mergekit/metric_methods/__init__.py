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

from mergekit.metric_methods.base import MetricMethod
from mergekit.metric_methods.PCA_rank import PCA_RankMetric
from mergekit.metric_methods.MSE import MSEMetric
from mergekit.metric_methods.all_metrics import AllMetric


def get(method: str) -> MetricMethod:
    if method == "PCA_rank":
        return PCA_RankMetric()
    elif method == "MSE":
        return MSEMetric()
    elif method == "all":
        return AllMetric()
    raise RuntimeError(f"Unimplemented metric method {method}")


__all__ = [
    "MetricMethod",
    "get",
    "MSEMetric",
    "PCA_RankMetric",
]
