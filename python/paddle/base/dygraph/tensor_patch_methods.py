# Copyright (c) 2019 PaddlePaddle Authors. All Rights Reserved.
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

import hashlib
import inspect
import sys
import warnings

import numpy as np

import paddle
from paddle import _C_ops, profiler
from paddle.base.data_feeder import (
    _PADDLE_DTYPE_2_NUMPY_DTYPE,
    convert_uint16_to_float,
)
from paddle.profiler.utils import in_profiler_mode
from paddle.utils import deprecated

from .. import core, framework, unique_name
from ..framework import (
    EagerParamBase,
    Parameter,
    Variable,
    convert_np_dtype_to_dtype_,
)
from .base import switch_to_static_graph
from .math_op_patch import monkey_patch_math_tensor

_grad_scalar = None


class TensorHookRemoveHelper:
    """
    A helper class that for removing Tensor gradient's hook.
    NOTE(wuweilong):the operation weakref.ref(tensor) will cause some unexpected errors in eager mode.
    """

    def __init__(self, tensor, hook_id):
        self._tensor = tensor
        self._hook_id = hook_id

    def remove(self):
        """
        Remove reference Tensor's hook.

        Returns:
            bool: Return True if removed successfully
        """
        tensor = self._tensor
        if tensor is not None:
            res = tensor._remove_grad_hook(self._hook_id)
            if res is True:
                return True
            else:
                warnings.warn(
                    "The backward hook (ID: %d) of Tensor `%s` you want to remove does not exist or has been removed."
                    % (self._hook_id, tensor.name),
                    RuntimeWarning,
                )
        return False


_already_patch_repr = False


def monkey_patch_tensor():
    @switch_to_static_graph
    def _to_static_var(self, to_parameter=False, **kwargs):
        """
        **Notes**:
            **This API is ONLY available in Dygraph mode**

        Transform a Tensor into static Variable with same attributes. It's a low level interface used
        in dy2static and shall not be called directly.

        Args:
            to_parameter (bool): It takes effect only if the input a Tensor. If set True,
                                 the Tensor will be converted into framework.Parameters. Otherwise, it will
                                 be converted into framework.Variable. Default False.

        Examples:
            .. code-block:: python

                >>> import paddle.base as base
                >>> from paddle.base.dygraph.base import to_variable
                >>> import numpy as np

                >>> data = np.ones([3, 1024], dtype='float32')
                >>> with base.dygraph.guard():
                ...     tensor = to_variable(data)
                ...     static_var = tensor._to_static_var()
        """

        # Note: getattr(self, attr, None) will call x.grad=x.gradient(), but gradient() only available in dygraph.
        # It will fail. So, for propery that different between dynamic and static graph, should not getattr(self, attr, None).
        attr_not_need_keys = [
            'grad',
            'T',
            'place',
            '_place_str',
            'data',
            'grad_',
            'strides',
            'offset',
        ]
        param_keys = ['stop_gradient', 'trainable']
        if isinstance(self, EagerParamBase):
            attr_kwargs = self.__dict__.copy()
            for key in param_keys:
                attr_kwargs[key] = getattr(self, key)
        else:
            attr_names = []
            for name in dir(self):
                if name not in attr_not_need_keys:
                    if not inspect.ismethod(
                        getattr(self, name)
                    ) and not name.startswith('_'):
                        attr_names.append(name)
            attr_kwargs = {name: getattr(self, name) for name in attr_names}

        attr_keys = ['block', 'shape', 'dtype', 'type', 'name', 'persistable']
        for attr in attr_keys:
            attr_kwargs[attr] = getattr(self, attr, None)

        # If specify block, use it instead of self.block
        if 'block' in kwargs:
            attr_kwargs['block'] = kwargs['block']

        attr_kwargs.update(kwargs)

        if to_parameter or isinstance(self, EagerParamBase):
            del attr_kwargs['persistable']
            # NOTE(Aurelius84): All parameters should be placed into global block.
            attr_kwargs['block'] = attr_kwargs['block'].program.global_block()
            static_var = Parameter(**attr_kwargs)
        else:
            static_var = Variable(**attr_kwargs)

        if self.dist_attr is not None:  # import for shard tensor api
            import paddle.distributed as dist

            static_var = dist.shard_tensor(
                static_var,
                self.dist_attr.process_mesh,
                self.placements,
                stop_gradient=static_var.stop_gradient,
            )
        return static_var

    # TODO(jiabin): move this to cplusplus end if we find some performance issue on it
    @framework.dygraph_only
    def set_value(self, value):
        """
        **Notes**:
            **This API is ONLY available in Dygraph mode**

        Set a new value for this Variable.

        Args:
            value (Variable|np.ndarray): the new value.

        Examples:
            .. code-block:: python

                >>> import paddle.base as base
                >>> from paddle.base.dygraph.base import to_variable
                >>> from paddle.nn import Linear
                >>> import numpy as np

                >>> data = np.ones([3, 1024], dtype='float32')
                >>> with base.dygraph.guard():
                ...     linear = Linear(1024, 4)
                ...     t = to_variable(data)
                ...     linear(t)  # call with default weight
                ...     custom_weight = np.random.randn(1024, 4).astype("float32")
                ...     linear.weight.set_value(custom_weight)  # change existing weight
                ...     out = linear(t)  # call with different weight
        """
        base_tensor = core.eager.Tensor
        assert isinstance(
            value, (np.ndarray, base_tensor, dict, str)
        ), "Variable set_value function, arguments type only support Variable, numpy, Tensor, dict, string."
        if self.is_dist():
            assert isinstance(
                value, (np.ndarray, base_tensor)
            ), "For set_value function of dist tensor, arguments type only support numpy or Tensor."

        if isinstance(value, (dict, str)):
            assert len(self) == len(
                value
            ), "Variable length not match, Variable [ {} ] need tensor with length {} but load set tensor with length {}".format(
                self.name, len(self), len(value)
            )
            if isinstance(value, dict):
                self.value().set_vocab(value)
            else:
                self.value().set_string_list(value)
        else:
            assert self.shape == list(
                value.shape
            ), "Variable Shape not match, Variable [ {} ] need tensor with shape {} but load set tensor with shape {}".format(
                self.name, self.shape, value.shape
            )

            if isinstance(value, base_tensor):
                dtype = value.dtype
            else:
                dtype = convert_np_dtype_to_dtype_(value.dtype)

            assert (
                self.dtype == dtype
            ), "Variable dtype not match, Variable [ {} ] need tensor with dtype {}  but load tensor with dtype {}".format(
                self.name, self.dtype, dtype
            )

            # NOTE(wuweilong): self could be Tensor, the subsequent behavior are defined in different files
            # if self is Tensor, method value() return self that defined in this file, get_tensor() defined in eager_method.cc
            # this Interface behavior will be unifed in the future.
            if self.is_dist():
                # calling set method bound for DistTensor
                value = paddle.distributed.shard_tensor(
                    value, self.value().process_mesh, self.value().placements
                )
                self.value().get_tensor().set(value.get_tensor())
                return
            self.value().get_tensor().set(
                value, framework._current_expected_place()
            )

    @framework.dygraph_only
    def backward(self, grad_tensor=None, retain_graph=False):
        """
        Run backward of current Graph which starts from current Tensor.

        The new gradient will accumulate on previous gradient.

        You can clear gradient by ``Tensor.clear_grad()`` .

        Args:
            grad_tensor(Tensor, optional): initial gradient values of the current Tensor. If `grad_tensor` is None,
            the initial gradient values of the current Tensor would be Tensor filled with 1.0;
            if `grad_tensor` is not None, it must have the same length as the current Tensor.
            The default value is None.

            retain_graph(bool, optional): If False, the graph used to compute grads will be freed. If you would
                like to add more ops to the built graph after calling this method( :code:`backward` ), set the parameter
                :code:`retain_graph` to True, then the grads will be retained. Thus, setting it to False is much more memory-efficient.
                Defaults to False.
        Returns:
            NoneType: None

        Examples:
            .. code-block:: python

                >>> import paddle
                >>> x = paddle.to_tensor(5., stop_gradient=False)
                >>> for i in range(5):
                ...     y = paddle.pow(x, 4.0)
                ...     y.backward()
                ...     print("{}: {}".format(i, x.grad))
                0: Tensor(shape=[], dtype=float32, place=Place(cpu), stop_gradient=False,
                500.)
                1: Tensor(shape=[], dtype=float32, place=Place(cpu), stop_gradient=False,
                1000.)
                2: Tensor(shape=[], dtype=float32, place=Place(cpu), stop_gradient=False,
                1500.)
                3: Tensor(shape=[], dtype=float32, place=Place(cpu), stop_gradient=False,
                2000.)
                4: Tensor(shape=[], dtype=float32, place=Place(cpu), stop_gradient=False,
                2500.)

                >>> x.clear_grad()
                >>> print("{}".format(x.grad))
                Tensor(shape=[], dtype=float32, place=Place(cpu), stop_gradient=False,
                0.)

                >>> grad_tensor=paddle.to_tensor(2.)
                >>> for i in range(5):
                ...     y = paddle.pow(x, 4.0)
                ...     y.backward(grad_tensor)
                ...     print("{}: {}".format(i, x.grad))
                0: Tensor(shape=[], dtype=float32, place=Place(cpu), stop_gradient=False,
                1000.)
                1: Tensor(shape=[], dtype=float32, place=Place(cpu), stop_gradient=False,
                2000.)
                2: Tensor(shape=[], dtype=float32, place=Place(cpu), stop_gradient=False,
                3000.)
                3: Tensor(shape=[], dtype=float32, place=Place(cpu), stop_gradient=False,
                4000.)
                4: Tensor(shape=[], dtype=float32, place=Place(cpu), stop_gradient=False,
                5000.)
        """
        if framework.in_dygraph_mode():
            if in_profiler_mode():
                record_event = profiler.RecordEvent(
                    "Gradient Backward", profiler.TracerEventType.Backward
                )
                record_event.begin()
            if grad_tensor is not None:
                assert isinstance(
                    grad_tensor, core.eager.Tensor
                ), "The type of grad_tensor must be paddle.Tensor"

                assert (
                    grad_tensor.shape == self.shape
                ), "Tensor shape not match, Tensor of grad_tensor [ {} ] with shape {} mismatch Tensor [ {} ] with shape {}".format(
                    grad_tensor.name, grad_tensor.shape, self.name, self.shape
                )

            if grad_tensor is None:
                grad_tensor = []
            else:
                grad_tensor = [grad_tensor]
            if _grad_scalar:
                # When using amp with Fleet DistributedStrategy, we do loss scaling implicitly.
                self = _grad_scalar.scale(self)

            core.eager.run_backward([self], grad_tensor, retain_graph)

            if in_profiler_mode():
                record_event.end()
        else:
            raise ValueError(
                "Variable.backward() is only available in DyGraph mode"
            )

    @framework.dygraph_only
    @deprecated(
        since="2.1.0",
        level=1,
        reason="Please use tensor.grad, which returns the tensor value of the gradient.",
    )
    def gradient(self):
        """
        .. warning::
          This API will be deprecated in the future, it is recommended to use
          :code:`x.grad` which returns the tensor value of the gradient.

        Get the Gradient of Current Tensor.

        Returns:
            ndarray: Numpy value of the gradient of current Tensor

        Examples:
            .. code-block:: python

                >>> import paddle

                >>> x = paddle.to_tensor(5., stop_gradient=False)
                >>> y = paddle.pow(x, 4.0)
                >>> y.backward()
                >>> print("grad of x: {}".format(x.gradient()))
                grad of x: 500.0

        """
        if self.grad is None:
            return None
        if self.grad.is_selected_rows():
            return (np.array(self.grad), np.array(self.grad.rows()))
        return np.array(self.grad)

    @framework.dygraph_only
    def register_hook(self, hook):
        """
        Registers a backward hook for current Tensor.

        The hook will be called every time the gradient Tensor of current Tensor is computed.

        The hook should not modify the input gradient Tensor, but it can optionally return
        a new gradient Tensor which will be used in place of current Tensor's gradient.

        The hook should have the following signature:

            hook(grad) -> Tensor or None

        Args:
            hook(function): A backward hook to be registered for Tensor.grad

        Returns:
            TensorHookRemoveHelper: A helper object that can be used to remove the registered hook by calling `remove()` method.

        Examples:
            .. code-block:: python

                >>> import paddle

                >>> # hook function return None
                >>> def print_hook_fn(grad):
                ...     print(grad)
                ...
                >>> # hook function return Tensor
                >>> def double_hook_fn(grad):
                ...     grad = grad * 2
                ...     return grad
                ...
                >>> x = paddle.to_tensor([0., 1., 2., 3.], stop_gradient=False)
                >>> y = paddle.to_tensor([4., 5., 6., 7.], stop_gradient=False)
                >>> z = paddle.to_tensor([1., 2., 3., 4.])

                >>> # one Tensor can register multiple hooks
                >>> h = x.register_hook(print_hook_fn)
                >>> x.register_hook(double_hook_fn)

                >>> w = x + y
                >>> # register hook by lambda function
                >>> w.register_hook(lambda grad: grad * 2)

                >>> o = z.matmul(w)
                >>> o.backward()
                >>> # print_hook_fn print content in backward
                Tensor(shape=[4], dtype=float32, place=Place(cpu), stop_gradient=False,
                [2., 4., 6., 8.])

                >>> print("w.grad:", w.grad)
                w.grad: None
                >>> print("x.grad:", x.grad)
                x.grad: Tensor(shape=[4], dtype=float32, place=Place(cpu), stop_gradient=False,
                [4. , 8. , 12., 16.])
                >>> print("y.grad:", y.grad)
                y.grad: Tensor(shape=[4], dtype=float32, place=Place(cpu), stop_gradient=False,
                [2., 4., 6., 8.])

                >>> # remove hook
                >>> h.remove()
        """
        if self.stop_gradient is True:
            raise RuntimeError(
                "Cannot register hook on a tensor that stop gradient."
            )

        hook_id = self._register_grad_hook(hook)
        helper = TensorHookRemoveHelper(self, hook_id)
        return helper

    @framework.dygraph_only
    def _to(self, device=None, dtype=None, blocking=None):
        if device is None and dtype is None and blocking is None:
            return self

        if device is not None:
            if isinstance(device, str):
                device = paddle.device._convert_to_place(device)
            elif isinstance(
                device,
                (
                    core.CPUPlace,
                    core.CUDAPlace,
                    core.CUDAPinnedPlace,
                    core.XPUPlace,
                    core.CustomPlace,
                ),
            ):
                pass
            else:
                raise ValueError(
                    "device value error, must be str, paddle.CPUPlace(), paddle.CUDAPlace(), paddle.CUDAPinnedPlace(), paddle.XPUPlace() or paddle.CustomPlace(), but the type of device is "
                    + type(device).__name__
                )

        if blocking is None:
            blocking = True
        else:
            assert isinstance(
                blocking, bool
            ), "blocking value error, must be the True, False or None"

        def transform(t, device, dtype, blocking):
            if device is None:
                device = t.place
            if dtype is None:
                dtype = t.dtype
            if type(dtype) is str:
                dtype = framework.convert_np_dtype_to_dtype_(dtype)

            # 1. gpu place need to determine whether the memory is sufficient for allocation.
            if t.place.is_gpu_place():
                size_dtype = core.size_of_dtype(dtype)
                # Note(weilong wu): Paddle GPU minimum memory allocation unit is 256 bytes,
                # waiting_alloc_memory will compute the memory space occupied by 't'.
                # Coefficient 1.2 is used to avoid OOM that may occur in this critical state when the memory is just enough.
                waiting_alloc_memory = (
                    ((t._numel() * size_dtype) / 256 + 1) * 256 * 1.2
                )
                gpu_memory_available = core.gpu_memory_available()
                if gpu_memory_available < waiting_alloc_memory:
                    # Copy Tensor to cpu
                    t_used = t._copy_to(paddle.CPUPlace(), blocking)
                    # Release memory of t
                    t._clear()
                else:
                    # Tensor still in GPU
                    t_used = t
            else:
                t_used = t

            # 2. cast Tensor to dtype
            if dtype is not None and dtype != t_used.dtype:
                with paddle.base.framework._dygraph_place_guard(
                    place=t_used.place
                ):
                    t_casted = t_used.cast(dtype=dtype)
            else:
                t_casted = t_used

            # 3. Copy casted Tensor(in CPU or GPU) to device
            if device is not None and not t_casted.place._equals(device):
                new_t = t_casted._copy_to(device, blocking)
            else:
                new_t = t_casted

            # 4. Share Tensor to origin Tensor
            dst_tensor = t.value().get_tensor()
            src_tensor = new_t.value().get_tensor()
            dst_tensor._share_data_with(src_tensor)

            return t

        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=UserWarning)
            return transform(self, device, dtype, blocking)

    @framework.dygraph_only
    def to(self, *args, **kwargs):
        """
        Performs Tensor dtype and/or device conversion. A paddle.dtype and place
        are inferred from the arguments of ``self.to(*args, **kwargs)``.There are
        three ways to call `to`:

            1. to(dtype, blocking=True)
            2. to(device, dtype=None, blocking=True)
            3. to(other, blocking=True)

        Returns:
            Tensor: self

        Examples:
            .. code-block:: python

                >>> import paddle
                >>> tensorx = paddle.to_tensor([1,2,3])
                >>> print(tensorx)
                Tensor(shape=[3], dtype=int64, place=Place(gpu:0), stop_gradient=True,
                    [1, 2, 3])

                >>> tensorx = tensorx.to("cpu")
                >>> print(tensorx.place)
                Place(cpu)

                >>> tensorx = tensorx.to("float32")
                >>> print(tensorx.dtype)
                paddle.float32

                >>> tensorx = tensorx.to("gpu", "int16")
                >>> print(tensorx)
                Tensor(shape=[3], dtype=int16, place=Place(gpu:0), stop_gradient=True,
                    [1, 2, 3])
                >>> tensor2 = paddle.to_tensor([4,5,6])
                >>> tensor2
                Tensor(shape=[3], dtype=int64, place=Place(gpu:0), stop_gradient=True,
                    [4, 5, 6])
                >>> tensor2 = tensor2.to(tensorx)
                >>> print(tensor2)
                Tensor(shape=[3], dtype=int16, place=Place(gpu:0), stop_gradient=True,
                    [4, 5, 6])
        """
        device = None
        dtype = None
        blocking = None
        size_args = len(args)
        size_kwargs = len(kwargs)

        def get_device_dtype_from_tensor(other):
            if other is not None:
                device = str(other.place)[6:-1]
                dtype = other.dtype
                return device, dtype
            else:
                return None, None

        if size_args + size_kwargs > 3 or size_args + size_kwargs == 0:
            raise TypeError(
                "to() received too mant arguments - expected one of:\n  \
                * (Union[str, paddle.CPUPlace(), paddle.CUDAPlace(), paddle.CUDAPinnedPlace(), paddle.XPUPlace(), paddle.CustomPlace()] \
                device, Union[str, paddle.dtype, numpy.dtype] dtype, bool blocking)\n \
                * (Union[str, paddle.dtype, numpy.dtype] dtype, bool blocking)\n \
                * (paddle.Tensor other, bool blocking) "
            )
        valid_keys = {"device", "dtype", "blocking", "other"}
        valid_dtypes = [
            "bfloat16",
            "float16",
            "float32",
            "float64",
            "int8",
            "int16",
            "int32",
            "int64",
            "uint8",
            "complex64",
            "complex128",
            "bool",
        ]
        invalid_keys = set(kwargs.keys()) - valid_keys
        if len(invalid_keys) != 0:
            raise TypeError(
                "to() got an unexpected keyword argument "
                + list(invalid_keys)[0]
            )
        if size_args > 0:
            if isinstance(args[0], paddle.Tensor):
                device, dtype = get_device_dtype_from_tensor(args[0])
                if size_args == 2:
                    blocking = args[1]
                else:
                    blocking = kwargs.get("blocking", None)
            elif (
                isinstance(args[0], (paddle.dtype, np.dtype))
                or isinstance(args[0], str)
                and args[0].lower() in valid_dtypes
            ):
                dtype = args[0]
                if size_args == 2:
                    blocking = args[1]
                else:
                    blocking = kwargs.get("blocking", None)
            else:
                device = args[0]
                if size_args == 2:
                    dtype = args[1]
                elif size_args == 3:
                    dtype, blocking = args[1], args[2]
                else:
                    dtype = kwargs.get("dtype", None)
                    blocking = kwargs.get("blocking", None)
        else:
            device = kwargs.get("device", None)
            dtype = kwargs.get("dtype", None)
            blocking = kwargs.get("blocking", None)
            if device is None and dtype is None:
                device, dtype = get_device_dtype_from_tensor(
                    kwargs.get("other", None)
                )
        return self._to(device, dtype, blocking)

    @property
    def grad(self):
        """
        .. warning::
          This API will return the tensor value of the gradient. If you want
          to get the numpy value of the gradient, you can use :code:`x.grad.numpy()`.

        Get the Gradient of Current Tensor.

        Returns:
            Tensor: the gradient of current Tensor

        Examples:
            .. code-block:: python

                >>> import paddle

                >>> x = paddle.to_tensor(5., stop_gradient=False)
                >>> y = paddle.pow(x, 4.0)
                >>> y.backward()
                >>> print("grad of x: {}".format(x.grad))
                grad of x: Tensor(shape=[], dtype=float32, place=CUDAPlace(0), stop_gradient=False, 500.)

        """
        msg = (
            'tensor.grad will return the tensor value of the gradient.'
            ' This is an incompatible upgrade for tensor.grad API. '
            ' It\'s return type changes from numpy.ndarray in version 2.0 to paddle.Tensor in version 2.1.0. '
            ' If you want to get the numpy value of the gradient, you can use :code:`x.grad.numpy()`'
        )
        warning_msg = "\033[93m\nWarning:\n%s \033[0m" % (msg)
        # ensure ANSI escape sequences print correctly in cmd and powershell
        if sys.platform.lower() == 'win32':
            warning_msg = "\nWarning:\n%s " % (msg)
        warnings.warn(warning_msg)
        return self._grad_ivar()

    def clear_grad(self):
        """
        The alias of clear_gradient().
        """
        self.clear_gradient()

    def item(self, *args):
        """
        Convert element at specific position in Tensor into Python scalars. If the position is not specified, the Tensor must be a
        single-element Tensor.

        Args:
            *args(int): The input coordinates. If it's single int, the data in the corresponding order of flattened Tensor will be returned.
                Default: None, and it must be in the case where Tensor has only one element.

        Returns(Python scalar): A Python scalar, whose dtype is corresponds to the dtype of Tensor.

        Raises:
            ValueError: If the Tensor has more than one element, there must be coordinates.

        Examples:
            .. code-block:: python

                >>> import paddle

                >>> x = paddle.to_tensor(1)
                >>> print(x.item())
                1
                >>> print(type(x.item()))
                <class 'int'>

                >>> x = paddle.to_tensor(1.0)
                >>> print(x.item())
                1.0
                >>> print(type(x.item()))
                <class 'float'>

                >>> x = paddle.to_tensor(True)
                >>> print(x.item())
                True
                >>> print(type(x.item()))
                <class 'bool'>

                >>> x = paddle.to_tensor(1+1j)
                >>> print(x.item())
                (1+1j)
                >>> print(type(x.item()))
                <class 'complex'>

                >>> x = paddle.to_tensor([[1.1, 2.2, 3.3]])
                >>> print(x.item(2))
                3.299999952316284
                >>> print(x.item(0, 2))
                3.299999952316284

        """
        scalar = self._getitem_from_offset(*args)
        if scalar.dtype == np.uint16:
            return convert_uint16_to_float(scalar).item()
        return scalar.item()

    @property
    def inplace_version(self):
        """
        The inplace version of current Tensor.
        The version number is incremented whenever the current Tensor is modified through an inplace operation.

        **Notes: This is a read-only property**

        Examples:
            .. code-block:: python

                >>> import paddle
                >>> var = paddle.ones(shape=[4, 2, 3], dtype="float32")
                >>> print(var.inplace_version)
                0

                >>> var[1] = 2.2
                >>> print(var.inplace_version)
                1

        """
        return self._inplace_version()

    def __str__(self):
        """
        Convert a Tensor object to a readable string.

        Returns(str): A readable string.

        Examples:
            .. code-block:: python

                >>> import paddle
                >>> paddle.seed(2023)
                >>> x = paddle.rand([2, 5])
                >>> print(x)
                Tensor(shape=[2, 5], dtype=float32, place=Place(cpu), stop_gradient=True,
                [[0.86583614, 0.52014720, 0.25960937, 0.90525323, 0.42400089],
                 [0.40641287, 0.97020894, 0.74437362, 0.51785129, 0.73292869]])
        """
        from paddle.tensor.to_string import tensor_to_string

        return tensor_to_string(self)

    def __deepcopy__(self, memo):
        """
        Deep copy Tensor, it will always performs Tensor copy.

        Examples:
            .. code-block:: python

                >>> import paddle
                >>> import copy
                >>> x = paddle.to_tensor(2.)
                >>> y = copy.deepcopy(x)
                >>> print(x)
                Tensor(shape=[], dtype=float32, place=Place(cpu), stop_gradient=True,
                2.)
                >>> print(y)
                Tensor(shape=[], dtype=float32, place=Place(cpu), stop_gradient=True,
                2.)
        """
        new_tensor = core.eager.Tensor()
        new_tensor.name = self.name + unique_name.generate("_deepcopy")
        memo[id(self)] = new_tensor
        new_tensor.copy_(self, True)
        return new_tensor

    @property
    def block(self):
        return framework.default_main_program().global_block()

    def __nonzero__(self):
        # np.prod([]) -> np.float64, so use int
        numel = int(np.prod(self.shape))
        assert (
            numel == 1
        ), "When Variable is used as the condition of if/while , Variable can only contain one element."
        assert self._is_initialized(), "tensor not initialized"
        return bool(np.array(self) > 0)

    def __bool__(self):
        return self.__nonzero__()

    def __array__(self, dtype=None):
        """
        Returns a numpy array shows the value of current Tensor.

        Returns:
            ndarray: The numpy value of current Tensor.

        Returns type:
            ndarray: dtype is same as current Tensor

        Examples:
            .. code-block:: python

                >>> import paddle
                >>> import numpy as np
                >>> x = paddle.randn([2, 2])
                >>> x_array = np.array(x)

                >>> print(type(x_array))
                <class 'numpy.ndarray'>
                >>> print(x_array.shape)
                (2, 2)
        """
        array = self.numpy(False)
        if dtype:
            array = array.astype(dtype)
        return array

    def pre_deal_index_and_value(self, item, value=None):
        # since in pybind there is no effiency way to transfer Py_Tuple/Py_List/Py_Range to Tensor
        # we call this function in python level.
        item = list(item) if isinstance(item, tuple) else [item]
        for i, slice_item in enumerate(item):
            if isinstance(slice_item, (list, np.ndarray, tuple)):
                item[i] = paddle.to_tensor(slice_item)
            elif isinstance(slice_item, range):
                item[i] = paddle.to_tensor(list(slice_item))

        if value is not None and not isinstance(value, Variable):
            value = paddle.to_tensor(value, dtype=self.dtype)

        return tuple(item), value

    def __getitem__(self, item):
        item, _ = pre_deal_index_and_value(self, item)
        return self._getitem_dygraph(item)

    def __setitem__(self, item, value):
        item, value = pre_deal_index_and_value(self, item, value)
        return self._setitem_dygraph(item, value)

    @framework.dygraph_only
    def _set_grad_ivar(self, value):
        if isinstance(self, EagerParamBase):
            self.grad = value
            self._unset_fake_empty()
        else:
            raise TypeError(
                "_set_grad_ivar is only supported for Parameter Tensor"
            )

    @framework.dygraph_only
    def value(self):
        return self

    @framework.dygraph_only
    def _slice(self, begin_idx, end_idx):
        return core.eager.Tensor(self.get_tensor()._slice(begin_idx, end_idx))

    @framework.dygraph_only
    def _numel(self):
        return self.get_tensor()._numel()

    @framework.dygraph_only
    def _clear_data(self):
        self.get_tensor()._clear()

    @framework.dygraph_only
    def _use_gpudnn(self, use_gpudnn=True):
        return self._tensor_use_gpudnn(use_gpudnn)

    @framework.dygraph_only
    def _uva(self, device_id=0):
        '''
        Returns self tensor with the UVA(unified virtual addressing).

        Args:
            device_id(int, optional): The destination GPU device id. Default: None, means current device.

        Examples:
            .. code-block:: python

                >>> # doctest: +REQUIRES(env:GPU)
                >>> import paddle
                >>> paddle.device.set_device('gpu')
                >>> x = paddle.to_tensor([1, 2, 3], place=paddle.CPUPlace())
                >>> x._uva()
                >>> print(x)
        '''
        self._tensor_uva(device_id)

    @framework.dygraph_only
    def cpu(self):
        if self.place.is_cpu_place():
            return self
        else:
            res = self._copy_to(core.CPUPlace(), True)
            res.stop_gradient = self.stop_gradient
            res.persistable = self.persistable
            return res

    @framework.dygraph_only
    def cuda(self, device_id=None, blocking=True):
        if device_id is None:
            res_place = framework._current_expected_place()
            if not isinstance(res_place, core.CUDAPlace):
                res_place = core.CUDAPlace(0)
        elif isinstance(device_id, int):
            res_place = core.CUDAPlace(device_id)
        else:
            raise ValueError("device_id must be int|None")

        if self.place._equals(res_place):
            return self
        else:
            res = self._copy_to(res_place, blocking)
            res.stop_gradient = self.stop_gradient
            res.persistable = self.persistable
            return res

    @framework.dygraph_only
    def pin_memory(self, blocking=True):
        if self.place.is_cuda_pinned_place():
            return self
        else:
            res = self._copy_to(core.CUDAPinnedPlace(), blocking)
            res.stop_gradient = self.stop_gradient
            res.persistable = self.persistable
            return res

    @framework.dygraph_only
    def values(self):
        """
        **Notes**:
            **This API is ONLY available in Dygraph mode**

        Get the values of current SparseTensor(COO or CSR).

        Returns:
            Tensor: A DenseTensor

        Examples:
            .. code-block:: python

                >>> import paddle
                >>> indices = [[0, 0, 1, 2, 2], [1, 3, 2, 0, 1]]
                >>> values = [1, 2, 3, 4, 5]
                >>> dense_shape = [3, 4]
                >>> sparse_x = paddle.sparse.sparse_coo_tensor(paddle.to_tensor(indices, dtype='int32'), paddle.to_tensor(values, dtype='float32'), shape=dense_shape)
                >>> print(sparse_x.values())
                Tensor(shape=[5], dtype=float32, place=Place(cpu), stop_gradient=True,
                [1., 2., 3., 4., 5.])
        """
        return _C_ops.sparse_values(self)

    @framework.dygraph_only
    def to_dense(self):
        """
        **Notes**:
            **This API is ONLY available in Dygraph mode**

        Convert the current SparseTensor(COO or CSR) to DenseTensor.

        Returns:
            Tensor: A DenseTensor

        Examples:
            .. code-block:: python

                >>> import paddle
                >>> indices = [[0, 0, 1, 2, 2], [1, 3, 2, 0, 1]]
                >>> values = [1, 2, 3, 4, 5]
                >>> dense_shape = [3, 4]
                >>> sparse_x = paddle.sparse.sparse_coo_tensor(paddle.to_tensor(indices, dtype='int64'), paddle.to_tensor(values, dtype='float32'), shape=dense_shape)
                >>> dense_x = sparse_x.to_dense()
                >>> print(dense_x)
                Tensor(shape=[3, 4], dtype=float32, place=Place(cpu), stop_gradient=True,
                [[0., 1., 0., 2.],
                 [0., 0., 3., 0.],
                 [4., 5., 0., 0.]])
        """

        return _C_ops.sparse_to_dense(self)

    @framework.dygraph_only
    def to_sparse_coo(self, sparse_dim):
        """
        **Notes**:
            **This API is ONLY available in Dygraph mode**

        Convert the current DenseTensor to SparseTensor in COO format.

        Returns:
            Tensor: A SparseCooTensor

        Examples:
            .. code-block:: python

                >>> import paddle
                >>> dense_x = [[0, 1, 0, 2], [0, 0, 3, 4]]
                >>> dense_x = paddle.to_tensor(dense_x, dtype='float32')
                >>> sparse_x = dense_x.to_sparse_coo(sparse_dim=2)
                >>> print(sparse_x)
                Tensor(shape=[2, 4], dtype=paddle.float32, place=Place(cpu), stop_gradient=True,
                       indices=[[0, 0, 1, 1],
                                [1, 3, 2, 3]],
                       values=[1., 2., 3., 4.])
        """

        return _C_ops.sparse_to_sparse_coo(self, sparse_dim)

    @framework.dygraph_only
    def _md5sum(self):
        """
        **Notes**:
            **This API is ONLY available in Dygraph mode**

        Calculate the md5sum of current Tensor.

        Returns:
            str: The md5sum of current Tensor.

        Examples:

            .. code-block:: python

                >>> import paddle
                >>> x = paddle.to_tensor([1, 2, 3])
                >>> print(x._md5sum())
                >>> #'1f68049372c5b2a4e0d049044450
        """
        numpy_array = np.array(self)
        array_bytes = numpy_array.tobytes()
        return hashlib.md5(array_bytes).hexdigest()

    def __hash__(self):
        return hash(id(self))

    @framework.dygraph_only
    def coalesce(self, name=None):
        r"""
        the coalesced operator include sorted and merge, after coalesced, the indices of x is sorted and unique.

        Parameters:
            x (Tensor): the input SparseCooTensor.
            name (str, optional): Name for the operation (optional, default is None).
                For more information, please refer to :ref:`api_guide_Name`.

        Returns:
            Tensor: return the SparseCooTensor after coalesced.

        Examples:
            .. code-block:: python

                >>> import paddle

                >>> indices = [[0, 0, 1], [1, 1, 2]]
                >>> values = [1.0, 2.0, 3.0]
                >>> sp_x = paddle.sparse.sparse_coo_tensor(indices, values)
                >>> sp_x = sp_x.coalesce()
                >>> print(sp_x.indices())
                Tensor(shape=[2, 2], dtype=int64, place=Place(cpu), stop_gradient=True,
                [[0, 1],
                [1, 2]])
                >>> print(sp_x.values())
                Tensor(shape=[2], dtype=float32, place=Place(cpu), stop_gradient=True,
                [3., 3.])
        """
        return _C_ops.sparse_coalesce(self)

    if not hasattr(core, "eager"):
        return

    for method_name, method in (
        ("__bool__", __bool__),
        ("__nonzero__", __nonzero__),
        ("_to_static_var", _to_static_var),
        ("set_value", set_value),
        ("block", block),
        ("backward", backward),
        ("clear_grad", clear_grad),
        ("inplace_version", inplace_version),
        ("gradient", gradient),
        ("register_hook", register_hook),
        ("__str__", __str__),
        ("__repr__", __str__),
        ("__deepcopy__", __deepcopy__),
        ("__module__", "paddle"),
        ("__array__", __array__),
        ("__getitem__", __getitem__),
        ("item", item),
        ("__setitem__", __setitem__),
        ("_to", _to),
        ("to", to),
        ("values", values),
        ("to_dense", to_dense),
        ("to_sparse_coo", to_sparse_coo),
        ("coalesce", coalesce),
        ("_set_grad_ivar", _set_grad_ivar),
        ("value", value),
        ("cpu", cpu),
        ("cuda", cuda),
        ("pin_memory", pin_memory),
        ("_slice", _slice),
        ("_numel", _numel),
        ("_uva", _uva),
        ("_clear_data", _clear_data),
        ("__hash__", __hash__),
        ("_use_gpudnn", _use_gpudnn),
        ("_md5sum", _md5sum),
    ):
        setattr(core.eager.Tensor, method_name, method)

    global _already_patch_repr
    if not _already_patch_repr:
        # NOTE(zhiqiu): pybind11 will set a default __str__ method of enum class.
        # So, we need to overwrite it to a more readable one.
        # See details in https://github.com/pybind/pybind11/issues/2537.
        origin = core.VarDesc.VarType.__str__

        def dtype_str(dtype):
            if dtype in _PADDLE_DTYPE_2_NUMPY_DTYPE:
                numpy_dtype = _PADDLE_DTYPE_2_NUMPY_DTYPE[dtype]
                if numpy_dtype == 'uint16':
                    numpy_dtype = 'bfloat16'
                prefix = 'paddle.'
                return prefix + numpy_dtype
            else:
                # for example, paddle.base.core.VarDesc.VarType.LOD_TENSOR
                return origin(dtype)

        core.VarDesc.VarType.__str__ = dtype_str
        _already_patch_repr = True

    # patch math methods for tensor
    monkey_patch_math_tensor()
