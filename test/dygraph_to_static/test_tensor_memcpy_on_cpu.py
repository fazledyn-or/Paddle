# Copyright (c) 2023 PaddlePaddle Authors. All Rights Reserved.
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

import unittest

import numpy as np
from dygraph_to_static_utils import (
    Dy2StTestBase,
    test_legacy_and_pt,
    test_legacy_and_pt_and_pir,
)

import paddle


def tensor_copy_to_cpu(x):
    x = paddle.to_tensor(x)
    y = x.cpu()
    return y


def tensor_copy_to_cuda(x):
    x = paddle.to_tensor(x)
    y = x.cuda()
    return y


def tensor_copy_to_cuda_with_warning(x, device_id=None, blocking=True):
    x = paddle.to_tensor(x)
    y = x.cuda(device_id, blocking)
    return y


class TestTensorCopyToCpuOnDefaultCPU(Dy2StTestBase):
    def _run(self, to_static):
        paddle.jit.enable_to_static(to_static)
        x1 = paddle.ones([1, 2, 3])
        x2 = paddle.jit.to_static(tensor_copy_to_cpu)(x1)
        return x1.place, x2.place, x2.numpy()

    @test_legacy_and_pt_and_pir
    def test_tensor_cpu_on_default_cpu(self):
        paddle.framework._set_expected_place(paddle.CPUPlace())
        dygraph_x1_place, dygraph_place, dygraph_res = self._run(
            to_static=False
        )
        static_x1_place, static_place, static_res = self._run(to_static=True)
        np.testing.assert_allclose(dygraph_res, static_res, rtol=1e-05)
        self.assertTrue(dygraph_x1_place.is_cpu_place())
        self.assertTrue(static_x1_place.is_cpu_place())
        self.assertTrue(dygraph_place.is_cpu_place())
        self.assertTrue(static_place.is_cpu_place())


class TestTensorCopyToCUDAOnDefaultCPU(Dy2StTestBase):
    def _run(self, to_static):
        paddle.jit.enable_to_static(to_static)
        x1 = paddle.ones([1, 2, 3])
        x2 = paddle.jit.to_static(tensor_copy_to_cuda)(x1)
        return x1.place, x2.place, x2.numpy()

    @test_legacy_and_pt
    def test_tensor_cuda_on_default_cpu(self):
        if not paddle.is_compiled_with_cuda():
            return

        """
        Note(liudongxue01): If the following asserts fail to run,
        please check the workaround logic for memcpy OP
        whether is still taking effect or not.
        See ConstructDeviceContext() in interpreter_util.cc.
        """
        paddle.framework._set_expected_place(paddle.CPUPlace())
        dygraph_x1_place, dygraph_place, dygraph_res = self._run(
            to_static=False
        )
        static_x1_place, static_place, static_res = self._run(to_static=True)
        np.testing.assert_allclose(dygraph_res, static_res, rtol=1e-05)
        self.assertTrue(dygraph_x1_place.is_cpu_place())
        self.assertTrue(static_x1_place.is_cpu_place())
        self.assertTrue(dygraph_place.is_gpu_place())
        self.assertTrue(static_place.is_gpu_place())


class TestTensorCopyToCUDAWithWarningOnCPU(unittest.TestCase):
    def _run(self, to_static):
        paddle.jit.enable_to_static(to_static)
        x1 = paddle.ones([1, 2, 3])
        x2 = paddle.jit.to_static(tensor_copy_to_cuda_with_warning)(
            x1, device_id=1, blocking=False
        )
        return x1.place, x2.place, x2.numpy()

    @test_legacy_and_pt_and_pir
    def test_with_warning_on_cpu(self):
        if not paddle.is_compiled_with_cuda():
            return

        paddle.framework._set_expected_place(paddle.CPUPlace())

        x1 = paddle.ones([1, 2, 3])
        with self.assertWarns(UserWarning, msg="ignored") as cm:
            x2 = paddle.jit.to_static(tensor_copy_to_cuda_with_warning)(
                x1, device_id=1, blocking=True
            )
        self.assertIn('math_op_patch.py', cm.filename)

        with self.assertWarns(UserWarning, msg="ignored") as cm:
            x2 = paddle.jit.to_static(tensor_copy_to_cuda_with_warning)(
                x1, device_id=None, blocking=False
            )
        self.assertIn('math_op_patch.py', cm.filename)

        with self.assertWarns(UserWarning, msg="ignored") as cm:
            x2 = paddle.jit.to_static(tensor_copy_to_cuda_with_warning)(
                x1, device_id=2, blocking=False
            )
        self.assertIn('math_op_patch.py', cm.filename)


if __name__ == '__main__':
    unittest.main()
