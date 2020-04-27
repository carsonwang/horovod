# Based on https://github.com/pytorch/pytorch/blob/master/torch/nn/modules/_functions.py
# Modifications copyright 2020 Maka Autonomous Robotic Systems
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
# ==============================================================================

from horovod.torch.mpi_ops import allgather_async, allreduce_async, Sum, size, synchronize

import torch
from torch.autograd.function import Function
import torch.nn.functional as F
from torch.nn.modules.batchnorm import _BatchNorm


class SyncBatchNorm(_BatchNorm):
    """
    Applies synchronous version of N-dimensional BatchNorm.  In this version, normalization
    parameters are synchronized across workers during forward pass.  This is very useful in
    situations where each GPU can fit a very small number of examples.

    See https://pytorch.org/docs/stable/nn.html#batchnorm2d for more details about BatchNorm.

    Arguments:
        num_features: number of channels `C` from the shape `(N, C, ...)`
        eps: a value added to the denominator for numerical stability. Default: 1e-5
        momentum: the value used for the running_mean and running_var
            computation. Can be set to `None` for cumulative moving average
            (i.e. simple average). Default: 0.1
        affine: a boolean value that when set to `True`, this module has
            learnable affine parameters. Default: `True`
        track_running_stats: a boolean value that when set to `True`, this
            module tracks the running mean and variance, and when set to `False`,
            this module does not track such statistics and always uses batch
            statistics in both training and eval modes. Default: `True`
    """
    def __init__(self, num_features, eps=1e-5, momentum=0.1, affine=True, track_running_stats=True):
        super().__init__(num_features, eps, momentum, affine, track_running_stats)

    def _check_input_dim(self, input):
        if input.dim() < 2:
            raise ValueError('expected at least 2D input (got {}D input)'.format(input.dim()))

    def forward(self, input):
        # currently only GPU input is supported
        if not input.is_cuda:
            raise ValueError('SyncBatchNorm expected input tensor to be on GPU')

        self._check_input_dim(input)

        if self.training and self.track_running_stats:
            self.num_batches_tracked = self.num_batches_tracked + 1

        if size() == 1 or (not self.training and self.track_running_stats):
            return F.batch_norm(
                input, self.running_mean, self.running_var, self.weight, self.bias,
                self.training or not self.track_running_stats, self.momentum, self.eps)
        else:
            return _SyncBatchNorm.apply(
                input, self.weight, self.bias, self.running_mean, self.running_var,
                self.eps, self.momentum)


class _SyncBatchNorm(Function):
    @staticmethod
    def forward(self, input, weight, bias, running_mean, running_var, eps, momentum):
        input = input.contiguous()

        size = input.numel() // input.size(1)
        if size == 1:
            raise ValueError('Expected more than 1 value per channel when training, got input size {}'.format(size))
        count = torch.tensor([size])

        # calculate mean/invstd for input.
        mean, invstd = torch.batch_norm_stats(input, eps)

        count_handle = allgather_async(count.unsqueeze(0), name='sync_batch_norm.count')
        mean_handle = allgather_async(mean.unsqueeze(0), name='sync_batch_norm.mean')
        invstd_handle = allgather_async(invstd.unsqueeze(0), name='sync_batch_norm.invstd')

        # wait on the async communication to finish
        count_all = synchronize(count_handle)
        mean_all = synchronize(mean_handle)
        invstd_all = synchronize(invstd_handle)

        # calculate global mean & invstd
        mean, invstd = torch.batch_norm_gather_stats_with_counts(
            input,
            mean_all,
            invstd_all,
            running_mean,
            running_var,
            momentum,
            eps,
            count_all.view(-1).tolist()
        )

        self.save_for_backward(input, weight, mean, invstd)

        # apply element-wise normalization
        return torch.batch_norm_elemt(input, weight, bias, mean, invstd, eps)

    @staticmethod
    def backward(self, grad_output):
        grad_output = grad_output.contiguous()
        saved_input, weight, mean, invstd = self.saved_tensors
        grad_input = grad_weight = grad_bias = None

        # calculate local stats as well as grad_weight / grad_bias
        mean_dy, mean_dy_xmu, grad_weight, grad_bias = torch.batch_norm_backward_reduce(
            grad_output,
            saved_input,
            mean,
            invstd,
            weight,
            self.needs_input_grad[0],
            self.needs_input_grad[1],
            self.needs_input_grad[2]
        )

        if self.needs_input_grad[0]:
            # synchronizing stats used to calculate input gradient.
            mean_dy_handle = allreduce_async(mean_dy, name='sync_batch_norm.mean_dy')
            mean_dy_xmu_handle = allreduce_async(mean_dy_xmu, name='sync_batch_norm.mean_dy_xmu')

            # wait on the async communication to finish
            mean_dy = synchronize(mean_dy_handle)
            mean_dy_xmu = synchronize(mean_dy_xmu_handle)

            # backward pass for gradient calculation
            grad_input = torch.batch_norm_backward_elemt(
                grad_output,
                saved_input,
                mean,
                invstd,
                weight,
                mean_dy,
                mean_dy_xmu
            )

        # synchronizing of grad_weight / grad_bias is not needed as distributed
        # training would handle all reduce.
        if weight is None or not self.needs_input_grad[1]:
            grad_weight = None

        if weight is None or not self.needs_input_grad[2]:
            grad_bias = None

        return grad_input, grad_weight, grad_bias, None, None, None, None, None, None
