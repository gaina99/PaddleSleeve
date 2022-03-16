# Copyright (c) 2021 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Provides classes to measure the distance between two images."""

from __future__ import division
from abc import ABC
from abc import abstractmethod
from functools import total_ordering
import numpy as np
from numbers import Number


@total_ordering
class Distance(ABC):
    """Base class for distances
    This class should be subclassed when implementing
    new distances. Subclasses must implement _calculate.
    """

    def __init__(
            self,
            reference=None,
            other=None,
            bounds=None,
            value=None):

        if value is not None:
            # alternative constructor
            assert isinstance(value, Number)
            assert reference is None
            assert other is None
            assert bounds is None
            self.reference = None
            self.other = None
            self._bounds = None
            self._value = value
            self._gradient = None
        else:
            # standard constructor
            self.reference = reference
            self.other = other
            self._bounds = bounds
            self._value, self._gradient = self._calculate()

        assert self._value is not None

    @property
    def value(self):
        return self._value

    @property
    def gradient(self):
        return self._gradient

    @abstractmethod
    def _calculate(self):
        """Returns distance and gradient of distance w.r.t. to self.other."""
        raise NotImplementedError

    def name(self):
        return self.__class__.__name__

    def __str__(self):
        return '{} = {:.6e}'.format(self.name(), self._value)

    def __repr__(self):
        return self.__str__()

    def __eq__(self, other):
        if other.__class__ != self.__class__:
            raise TypeError('Comparisons are only possible between the same distance types.')  # noqa: E501
        return self.value == other.value

    def __lt__(self, other):
        if other.__class__ != self.__class__:
            raise TypeError('Comparisons are only possible between the same distance types.')  # noqa: E501
        return self.value < other.value


class MeanSquaredDistance(Distance):
    """Calculates the mean squared error between two images."""

    def _calculate(self):
        min_, max_ = self._bounds
        n = self.reference.size
        f = n * (max_ - min_) ** 2

        diff = self.other - self.reference
        value = np.vdot(diff, diff) / f

        # calculate the gradient only when needed
        self._g_diff = diff
        self._g_f = f
        gradient = None
        return value, gradient

    @property
    def gradient(self):
        if self._gradient is None:
            self._gradient = self._g_diff / (self._g_f / 2)
        return self._gradient

    def __str__(self):
        return 'normalized MSE = {:.2e}'.format(self._value)


MSE = MeanSquaredDistance


class MeanAbsoluteDistance(Distance):
    """Calculates the mean absolute error between two images."""

    def _calculate(self):
        min_, max_ = self._bounds
        diff = (self.other - self.reference) / (max_ - min_)
        value = np.mean(np.abs(diff)).astype(np.float64)
        n = self.reference.size
        gradient = 1 / n * np.sign(diff) / (max_ - min_)
        return value, gradient

    def __str__(self):
        return 'normalized MAE = {:.2e}'.format(self._value)


MAE = MeanAbsoluteDistance


class Linfinity(Distance):
    """Calculates the L-infinity norm of the difference between two images."""

    def _calculate(self):
        min_, max_ = self._bounds
        diff = (self.other - self.reference) / (max_ - min_)
        value = np.max(np.abs(diff)).astype(np.float64)
        gradient = None
        return value, gradient

    @property
    def gradient(self):
        raise NotImplementedError

    def __str__(self):
        return 'normalized Linf distance = {:.2e}'.format(self._value)


Linf = Linfinity


class L0(Distance):
    """Calculates the L0 norm of the difference between two images."""

    def _calculate(self):
        diff = self.other - self.reference
        value = np.sum(diff != 0)
        gradient = None
        return value, gradient

    @property
    def gradient(self):
        raise NotImplementedError

    def __str__(self):
        return 'L0 distance = {}'.format(self._value)

    def __repr__(self):
        return self.__str__()

    def __eq__(self, other):
        if other.__class__ != self.__class__:
            raise TypeError('Comparisons are only possible between the same distance types.')  # noqa: E501
        return self.value == other.value

    def __lt__(self, other):
        if other.__class__ != self.__class__:
            raise TypeError('Comparisons are only possible between the same distance types.')  # noqa: E501
        return self.value < other.value
