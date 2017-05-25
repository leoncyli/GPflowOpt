# Copyright 2017 Joachim van der Herten
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from GPflow.param import Parameterized, AutoFlow, ParamList, DataHolder
from GPflow.model import Model
from GPflow import settings

import numpy as np

import tensorflow as tf

float_type = settings.dtypes.float_type
stability = settings.numerics.jitter_level


class Acquisition(Parameterized):
    """
    Class representing acquisition functions which map the predictive distribution of a Bayesian model into a score. In
    Bayesian Optimization this function is typically optimized over the optimization domain to determine the next
    point for evaluation.

    The acquisition function object maintains a list of models. Subclasses should implement a build_acquisition
    function which computes the acquisition function using tensorflow.

    Acquisition functions can be added or multiplied to construct joint criteria (for instance for constrained
    optimization)
    """

    def __init__(self, models=[], optimize_restarts=5):
        super(Acquisition, self).__init__()
        self.models = ParamList(np.atleast_1d(models).tolist())
        self._default_params = list(map(lambda m: m.get_free_state(), self.models))

        assert (optimize_restarts >= 0)
        self._optimize_restarts = optimize_restarts
        self._optimize_all()

    def _optimize_all(self):
        for model, hypers in zip(self.models, self._default_params):
            runs = []
            # Start from supplied hyperparameters
            model.set_state(hypers)
            for i in range(self._optimize_restarts):
                if i > 0:
                    model.randomize()
                try:
                    result = model.optimize()
                    runs.append(result)
                except tf.errors.InvalidArgumentError:
                    print("Warning - optimization restart {0}/{1} failed".format(i + 1, self._optimize_restarts))
            best_idx = np.argmin(map(lambda r: r.fun, runs))
            model.set_state(runs[best_idx].x)

    def _build_acquisition_wrapper(self, Xcand, gradients=True):
        acq = self.build_acquisition(Xcand)
        if gradients:
            return acq, tf.gradients(acq, [Xcand], name="acquisition_gradient")[0]
        else:
            return acq

    def set_data(self, X, Y):
        num_outputs_sum = 0
        for model in self.models:
            num_outputs = model.Y.shape[1]
            Ypart = Y[:, num_outputs_sum:num_outputs_sum + num_outputs]
            num_outputs_sum += num_outputs

            model.X = X
            model.Y = Ypart

        self._optimize_all()
        self.setup()
        return num_outputs_sum

    @property
    def data(self):
        if self._tf_mode:
            return self.models[0].X, tf.concat(list(map(lambda model: model.Y, self.models)), 1)
        else:
            return self.models[0].X.value, np.hstack(map(lambda model: model.Y.value, self.models))

    def constraint_indices(self):
        return np.empty((0,), dtype=int)

    def objective_indices(self):
        return np.setdiff1d(np.arange(self.data[1].shape[1]), self.constraint_indices())

    def setup(self):
        """
        Method triggered after calling set_data(). Override for pre-calculation of quantities used later in
        the evaluation of the acquisition function for candidate points
        """
        pass

    @AutoFlow((float_type, [None, None]))
    def evaluate_with_gradients(self, Xcand):
        return self._build_acquisition_wrapper(Xcand, gradients=True)

    @AutoFlow((float_type, [None, None]))
    def evaluate(self, Xcand):
        return self._build_acquisition_wrapper(Xcand, gradients=False)

    def __add__(self, other):
        if isinstance(other, AcquisitionSum):
            return AcquisitionSum([self, *other.operands.sorted_params])
        return AcquisitionSum([self, other])

    def __mul__(self, other):
        if isinstance(other, AcquisitionProduct):
            return AcquisitionProduct([self, *other.operands.sorted_params])
        return AcquisitionProduct([self, other])


class ExpectedImprovement(Acquisition):
    """
    Implementation of the Expected Improvement acquisition function for single-objective global optimization
    (Mockus et al, 1975)
    """

    def __init__(self, model):
        super(ExpectedImprovement, self).__init__(model)
        assert (isinstance(model, Model))
        self.fmin = DataHolder(np.zeros(1))
        self.setup()

    def setup(self):
        super(ExpectedImprovement, self).setup()
        # Obtain the lowest posterior mean for the previous evaluations
        samples_mean, _ = self.models[0].predict_f(self.data[0])
        self.fmin.set_data(np.min(samples_mean, axis=0))

    def build_acquisition(self, Xcand):
        # Obtain predictive distributions for candidates
        candidate_mean, candidate_var = self.models[0].build_predict(Xcand)
        candidate_var = tf.maximum(candidate_var, stability)

        # Compute EI
        normal = tf.contrib.distributions.Normal(candidate_mean, tf.sqrt(candidate_var))
        t1 = (self.fmin - candidate_mean) * normal.cdf(self.fmin)
        t2 = candidate_var * normal.pdf(self.fmin)
        return tf.add(t1, t2, name=self.__class__.__name__)


class ProbabilityOfFeasibility(Acquisition):
    """
    Probability of Feasibility acquisition function for learning  feasible regions
    """

    def __init__(self, model):
        super(ProbabilityOfFeasibility, self).__init__(model)

    def constraint_indices(self):
        return np.arange(self.data[1].shape[1])

    def build_acquisition(self, Xcand):
        candidate_mean, candidate_var = self.models[0].build_predict(Xcand)
        candidate_var = tf.maximum(candidate_var, stability)
        normal = tf.contrib.distributions.Normal(candidate_mean, tf.sqrt(candidate_var))
        return normal.cdf(tf.constant(0.0, dtype=float_type), name=self.__class__.__name__)


class AcquisitionAggregationOperator(Acquisition):
    def __init__(self, operands, oper):
        super(AcquisitionAggregationOperator, self).__init__()
        for operand in operands:
            assert isinstance(operand, Acquisition)
        self.operands = ParamList(operands)
        self._oper = oper

    @Acquisition.data.getter
    def data(self):
        X = self.operands[0].data[0]
        Ys = map(lambda operand: operand.data[1], self.operands)

        if self._tf_mode:
            return X, tf.concat(list(Ys), 1)
        else:
            return X, np.hstack(Ys)

    def set_data(self, X, Y):
        offset = 0
        for operand in self.operands:
            offset += operand.set_data(X, Y[:, offset:])
        return offset

    def constraint_indices(self):
        offset = [0]
        idx = []
        for operand in self.operands:
            idx.append(operand.constraint_indices())
            offset.append(operand.data[1].shape[1])
        return np.hstack([i + o for i, o in zip(idx, offset[:-1])])

    def build_acquisition(self, Xcand):
        return self._oper(tf.concat(list(map(lambda operand: operand.build_acquisition(Xcand), self.operands)), 1),
                          axis=1, keep_dims=True, name=self.__class__.__name__)

    def __getitem__(self, item):
        return self.operands[item]


class AcquisitionSum(AcquisitionAggregationOperator):
    def __init__(self, operands):
        super(AcquisitionSum, self).__init__(operands, tf.reduce_sum)

    def __add__(self, other):
        if isinstance(other, AcquisitionSum):
            return AcquisitionSum(self.operands.sorted_params + other.operands.sorted_params)
        else:
            return AcquisitionSum([*self.operands.sorted_params, other])


class AcquisitionProduct(AcquisitionAggregationOperator):
    def __init__(self, operands):
        super(AcquisitionProduct, self).__init__(operands, tf.reduce_prod)

    def __mul__(self, other):
        if isinstance(other, AcquisitionProduct):
            return AcquisitionProduct(self.operands.sorted_params + other.operands.sorted_params)
        else:
            return AcquisitionProduct([*self.operands.sorted_params, other])
