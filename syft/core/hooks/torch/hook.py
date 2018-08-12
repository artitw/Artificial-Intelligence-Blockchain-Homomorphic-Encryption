import torch
import inspect
import re
import json
import types
import functools
import importlib
from ... import workers
from ... import utils
from ..base import BaseHook
from .guard import TorchGuard


class TorchHook(BaseHook):
    r""" A Hook which Overrides Methods on PyTorch Variables & Tensors -
    **Currently compatible with PyTorch 0.3.1**

    The purpose of this class is to:

        * extend torch methods to allow for the moving of tensors
          and variables from one worker to another
        * override torch methods to execute commands on one worker
          that are called on tensors controlled by the local worker.

    This class is typically the first thing you will initialize when
    using PySyft with PyTorch because it is responsible for augmenting
    PyTorch with PySyft's added functionality (such as remote execution).

    :Parameters:

        * **local_worker (**:class:`.workers.BaseWorker` **, optional)**
          you can optionally provide a local worker as a parameter which
          TorchHook will assume to be the worker owned by the local machine.
          If you leave it empty, TorchClient will automatically initialize
          a :class:`.workers.VirtualWorker` under the assumption you're
          looking to do local experimentation/development.

        * **is_client (bool, optional)** whether or not the TorchHook is
          being initialized as an end-user client. This can impact whether
          or not variables are deleted when they fall out of scope. If you set
          this incorrectly on a end user client, Tensors and Variables will
          never be deleted. If you set this incorrectly on a remote machine
          (not a client), tensors will not get saved. It's really only
          important if you're not initializing the local worker yourself. (Default: True)

        * **verbose (bool, optional)** whether or not to print operations
          as they occur. (Defalt: True)

    :Example:

    >>> from syft.core.hooks import TorchHook
    >>> from syft.core.hooks import torch
    >>> hook = TorchHook()
    Hooking into Torch...
    Overloading Complete.
    >>> x = torch.FloatTensor([1,2,3,4,5])
    >>> x
     1
     2
     3
     4
     5
    [torch.FloatTensor of size 5]
    """

    def __init__(self, local_worker=None, is_client=True, verbose=True, queue_size=0):
        super().__init__()

        self.local_worker = local_worker
        if (self.local_worker is None):

            # Every TorchHook instance should have a local worker which is responsible for
            # interfacing with other workers. The worker interface is what allows the Torch
            # specific code in TorchHook to be agnostic to the means by which workers communicate
            # (such as peer-to-peer, sockets, through local ports, or all within the same process)

            if (hasattr(torch, 'local_worker')):
                self.local_worker = torch.local_worker
                if (verbose):
                    print("Torch seems to already have a local_worker object... \
                          using that one instead...")
            else:
                self.local_worker = workers.VirtualWorker(
                    hook=self, is_client_worker=is_client, queue_size=queue_size)
        else:
            # if the local_worker already exists, then it MUST not know about the hook which is
            # just being created. Thus, we must inform it.
            self.local_worker.hook = self

        torch.local_worker = self.local_worker

        # this is a list of all module functions in the torch module
        self.torch_funcs = dir(torch)

        # this is a list of all module functions in torch.nn.functional
        self.torch_functional_funcs = dir(torch.nn.functional)

        # this is the list of torch tensor types that we will override for remote execution
        self.tensor_types = [torch.FloatTensor,
                             torch.DoubleTensor,
                             torch.HalfTensor,
                             torch.ByteTensor,
                             torch.CharTensor,
                             torch.ShortTensor,
                             torch.IntTensor,
                             torch.LongTensor]

        # this is the list of torch VARIABLE types that we will override for remote execution
        # Variables are simply tensors that are differentiable (support gradients)
        # Parameters are Variables with requires_grad set to True by default
        # and are used heavily in the torch.nn package
        self.var_types = [torch.autograd.variable.Variable, torch.nn.Parameter]

        # a list of all classes in which we will override their methods for remote execution
        self.tensorvar_types = self.tensor_types + \
                               [torch.autograd.variable.Variable]
        self.tensorvar_types_strs = [x.__name__ for x in self.tensorvar_types]

        # a list of all methods in fixed precision type which will be overridden
        # for remote execution
        self.fixed_prec_var_methods = ['fixed_prec_add', 'fixed_prec_mul', 'fixed_prec_sub', 'fixed_prec_div',
                                   'set_precision', 'fixed_prec_trudiv', 'free_precision',
                                   '_execute_fixed_precision_call', '_conversion']

        self.tensorvar_methods = list(
            set(
                [method
                 for tensorvar in self.tensorvar_types
                 for method in dir(tensorvar)]
            )
        )

        # adding fixed precision methods to the list of overriding methods for remote execution
        self.tensorvar_methods.extend(self.fixed_prec_var_methods)

        # Methods that caused infinite recursion during testing
        # TODO: May want to handle the ones in "exclude" manually at
        #       some point
        self.exclude = (['ndimension', 'nelement', 'size', 'numel',
                         'type', 'tolist', 'dim', '__iter__', 'select'])

        # This one wasn't in dir(Variable) -- probably a C++ thing
        self.var_exclude = ['__getattr__']

        # Torch functions we don't want to override
        self.torch_exclude = ['save', 'load', 'typename']

        # Torch functions in torch.nn.functional we don't want to override
        self.torch_functional_exclude = []

        self.guard = TorchGuard()

        self.set_hooks(verbose)

    def set_hooks(self, verbose):
        """Overload functions in torch with our own versions to enable routing"""
        if (not hasattr(torch, 'hooked')):
            if (verbose):
                print('Hooking into Torch...')
            self._hook_torch_module()
            self._hook_torch_functional()
            for t_type in self.tensor_types:
                self._hook_tensor(t_type)
            self._hook_variable()
            self._hook_module()
            torch.hooked = True
            if (verbose):
                print('Overloading complete.')
        else:
            if (verbose):
                print("WARNING: Torch seems to be already overloaded... skipping...")

    def __enter__(self):
        """Allow for using TorchHook as a context manager"""
        if hasattr(torch, 'hooked'):
            self.previously_hooked = torch.hooked
        else:
            self.previously_hooked = False
        self.set_hooks(verbose=False)

    def __exit__(self):
        """When using TorchHook as a context manager,
        reimport the original module if torch wasn't previously hooked
        """
        if not self.previously_hooked:
            importlib.reload(torch)

    # ######## BEGIN GENERIC method/function hooking logic #########
    def _get_overload_method_in_tensor_or_var(hook_self, method):
        """Wrapper overloading partialmethod objects of Torch object
        methods.  Compiles command, checks for Tensors and Variables in
        the args/kwargs, determines locations of all Tensors and
        Variables involved in computation, and handles the computation
        accordingly.
        """

        @functools.wraps(method)
        def method_router(self, *args, **kwargs):
            """This is a routing function. If self is a local
            tensor (data stored locally), then it executes
            the call locally. If self is a remote tensor, it
            executes a call to a remote worker.
            """

            _method = method(self, *args, **kwargs)

            if hasattr(self, 'is_pointer') and self.is_pointer:
                return hook_self._execute_remote_call(_method,
                                                      has_self=True)[0]
            elif (hasattr(self, 'fixed_precision') and self.fixed_precision):

                return hook_self._execute_fixed_precision_call(self, _method, args, kwargs)

            else:
                return hook_self._execute_local_call(self, _method, args, kwargs)

        return method_router

    def _get_overload_function_in_torch_module(hook_self, func):
        """Wrapper overloading partial objects of functions in the torch
        module.  Compiles command, checks for Tensors and Variables in
        the args/kwargs, determines locations of all Tensors and
        Variables involved in computation, and handles the computation
        accordingly.
        """

        @functools.wraps(func)
        def function_router(*args, **kwargs):
            """This is a routing function. If self is a local
            tensor (data stored locally), then it executes
            the call locally. If self is a remote tensor, it
            executes a call to a remote worker.
            """
            part = func(*args, **kwargs)

            _res = hook_self._execute_remote_call(
                part, has_self=False)
            pointer, has_remote, multiple_owners = _res

            if (has_remote and not multiple_owners):
                return pointer

            if not (has_remote and not multiple_owners):
                result = hook_self._execute_local_call(None,
                                                       part,
                                                       args,
                                                       kwargs,
                                                       function_not_method=True)
                return result

        return function_router

    def _hook_fixed_precision_methods(self, tensor_type):
        """Overload fixed precision methods"""
        
        def set_precision(self, precision=5, encoding_type=torch.LongTensor):
            """Sets fixed precision value. Encoding type must be
            LongTensor or subclass of LongTensor. Fixed precision storage
            type is set to LongTensor.
            """

            if (issubclass(encoding_type, torch.LongTensor)):
                fixed_tensor = self.old_mul(10 ** precision).long()
                fixed_tensor.free_precision_parent = self
                fixed_tensor.fixed_precision = True
                fixed_tensor.precision = precision
                return fixed_tensor
            else:
                print("Fixed precision storage type", encoding_type, "not supported")

        def free_precision(self, decoding_type=torch.FloatTensor):
            """Remove fixed precision. Storage type is set to FloatTensor.
            Decoding type must be FloatTensor or subclass of FloatTensor.
            Returns self if precision has no precision fixed.
            """

            if (not self.fixed_precision):
                print("Tensor is not fixed precision but you called .free_precision()")
                return self

            if (issubclass(decoding_type, torch.FloatTensor)):
                free_tensor = self.float().old_div(10 ** self.precision)
                free_tensor.precision = -1
                free_tensor.fixed_precision = False

                if (hasattr(self, 'free_precision_parent')):
                    self.free_precision_parent.set_(free_tensor)
                    return self.free_precision_parent

                else:
                    return free_tensor
            else:
                TypeError("Decoding type", decoding_type, "not supported for free_precision")

            return self

        tensor_type.set_precision = set_precision
        tensor_type.free_precision = free_precision

        # Customized math operations
        def fixed_prec_mul(self, other, norm_left_prec=True):
            """Multiplication of input tensor with self. Both must have fixed precision.
            Raises OverflowError if combined precision of tensors
            exceed 17.  If norm_left_prec is set to false, output tensor
            takes precision of second argument, otherwise it takes precision
            of first argument.
            """

            if hasattr(self, 'fixed_precision') and hasattr(other, 'fixed_precision'):

                if self.precision + other.precision > 17:
                    raise OverflowError

                # modify tensor to be the precision of self
                if (norm_left_prec):
                    out = torch.LongTensor.old_mul(self, other).old_div(10 ** other.precision)
                    out.precision = self.precision
                    out.fixed_precision = True
                # modify tensor to be the precision of the other tensor
                else:
                    out = torch.LongTensor.old_mul(self, other).old_div(10 ** self.precision)
                    out.precision = other.precision
                    out.fixed_precision = True

                return out
            elif (hasattr(self, 'fixed_precision') and not hasattr(other, 'fixed_precision')) or \
                    (not hasattr(self,'fixed_precision') and hasattr(other, 'fixed_precision')):
                raise AttributeError("Tried to call fixed precision operation on non-fixed precision tensor and "
                                     "a regular tensor.")
            else:
                raise AttributeError("Tried to call fixed precision operation on non-fixed precision tensors")

        tensor_type.fixed_prec_mul = fixed_prec_mul

        def fixed_prec_add(self, other):
            """Addition of input tensor to self. Both must have
            fixed precision. Precision of the output tensor will 
            be the highest precision value between both tensors.
            """

            if hasattr(self, 'fixed_precision') and hasattr(self, 'fixed_precision'):

                if self.precision > other.precision:
                    out = torch.LongTensor.old_add(self, other.old_mul(10 ** (self.precision - other.precision)))
                    out.precision = self.precision
                    out.fixed_precision = True

                elif self.precision < other.precision:
                    out = torch.LongTensor.old_add(self.old_mul(10 ** (other.precision - self.precision)), other)
                    out.precision = other.precision
                    out.fixed_precision = True

                else:
                    out = torch.LongTensor.old_add(self, other)
                    out.precision = self.precision
                    out.fixed_precision = True

                return out
            elif (hasattr(self, 'fixed_precision') and not hasattr(other, 'fixed_precision')) or \
                 (not hasattr(self, 'fixed_precision') and hasattr(other, 'fixed_precision')):
                raise AttributeError("Tried to call fixed precision operation on non-fixed precision tensor and "
                                     "a regular tensor.")
            else:
                raise AttributeError("Tried to call fixed precision operation on non-fixed precision tensors")

        tensor_type.fixed_prec_add = fixed_prec_add


        def fixed_prec_sub(self, other):
            """Substraction of input tensor from self. Both must have
            fixed precision. Precision of the output tensor will 
            be the highest precision value between both tensors.
            """

            if hasattr(self, 'fixed_precision') and hasattr(self, 'fixed_precision'):

                if self.precision > other.precision:
                    out = torch.LongTensor.old_sub(self, other.old_mul(10 ** (self.precision - other.precision)))
                    out.precision = self.precision
                    out.fixed_precision = True

                elif self.precision < other.precision:
                    out = torch.LongTensor.old_sub(self.old_mul(10 ** (other.precision - self.precision)), other)
                    out.precision = other.precision
                    out.fixed_precision = True

                else:
                    out = torch.LongTensor.old_sub(self, other)
                    out.precision = self.precision
                    out.fixed_precision = True

                return out
            elif (hasattr(self, 'fixed_precision') and not hasattr(other, 'fixed_precision')) or \
                 (not hasattr(self, 'fixed_precision') and hasattr(other, 'fixed_precision')):
                raise AttributeError("Tried to call fixed precision operation on non-fixed precision tensor and "
                                     "a regular tensor.")
            else:
                raise AttributeError("Tried to call fixed precision operation on non-fixed precision tensors")

        tensor_type.fixed_prec_sub = fixed_prec_sub

        def fixed_prec_div(self, other):
            """Division of self by input tensor. Both must have
            fixed precision. Precision of the output tensor will 
            be the highest precision value between both tensors.
            """

            if hasattr(self, 'fixed_precision') and hasattr(self, 'fixed_precision'):


                if self.precision < other.precision:
                    out = torch.LongTensor.old_div(self.old_mul(10 ** (other.precision - self.precision)), other)
                    out.precision = 0
                    out.fixed_precision = True

                elif self.precision == other.precision:
                    out = torch.LongTensor.old_div(self, other)
                    out.precision = 0
                    out.fixed_precision = True

                else:
                    out = torch.LongTensor.old_div(self, other.old_mul(10 ** (self.precision - other.precision)))
                    out.precision = 0
                    out.fixed_precision = True

                return out

            elif (hasattr(self, 'fixed_precision') and not hasattr(other, 'fixed_precision')) or \
                (not hasattr(self, 'fixed_precision') and hasattr(other, 'fixed_precision')):
                    raise AttributeError("Tried to call fixed precision operation on non-fixed precision tensor and "
                            "a regular tensor.")
            else:
                raise AttributeError("Tried to call fixed precision operation on non-fixed precision tensors")

        tensor_type.fixed_prec_div = fixed_prec_div
        tensor_type.fixed_prec_trudiv = fixed_prec_div

    def _execute_fixed_precision_call(hookself, self, _method, args, kwargs):
        """Creates a fixed precision tensor"""

        def _conversion(self):
            """Returns converted tensor after math operations are applied"""

            if hasattr(self, 'precision'):
                return self.old_div(10 ** self.precision)
            else:
                raise AttributeError

        if (_method.func.__name__ == 'mul'):
            return self.fixed_prec_mul(*args, **kwargs)
        if (_method.func.__name__ == 'div'):
            return self.fixed_prec_div(*args, **kwargs)
        if (_method.func.__name__ == 'add'):
            return self.fixed_prec_add(*args, **kwargs)
        if (_method.func.__name__ == 'sub'):
            return self.fixed_prec_sub(*args, **kwargs)
        if (_method.func.__name__ == 'trudiv'):
            return self.fixed_prec_trudiv(*args, **kwargs)

        return _method.func(self, *args, **kwargs)

    def _execute_local_call(hook_self, self, _method, args, kwargs, function_not_method=False):
        """This executes a method locally"""

        if (function_not_method):
            result = _method.func(*args, **kwargs)
        else:
            result = _method.func(self, *args, **kwargs)

        # if the result hasn't been registered, register it
        if (type(result) in hook_self.tensorvar_types and (not hasattr(result, 'owner'))):
            result = hook_self.local_worker.register_object(result,
                                                            is_pointer=False)
        return result

    def _execute_remote_call(hook_self, _method, has_self=True):
        """This function is responsible for overloading all
        TENSOR and VARIABLE methods. Note that this is the
        method/function agnostic piece and is only called
        from within _overload_method and _overload_function
        """

        # Step 1/2: Compiles Command in JSON and retrieve Tensors and Variables
        command, tensorvars = hook_self._compile_command(_method, has_self=has_self)

        # Step 3: Checks to see if the tensor is local (on this machine) or is a pointer
        # to a remote one (on a different machine)
        has_remote = any([tensorvar.is_pointer for tensorvar in tensorvars])

        # Checks to see if the tensor has multiple owners (not yet fully supported func)
        owners = list(
            set([owner for tensorvar in tensorvars for owner in tensorvar.owners]))
        multiple_owners = len(owners) > 1

        # If one of the tensor arguments is remote
        # if the tensor only has one owner (remote)
        if has_remote and not multiple_owners:

            for worker in owners:
                responses = hook_self.local_worker.send_torch_command(recipient=worker,
                                                                     message=command)

                if not isinstance(responses, list):
                    responses = [responses]

                pointers = []
                for response in responses:
                    # Case 1: numeric response
                    if isinstance(response, dict) and 'numeric' in response.keys():
                        var_data = response['numeric']
                        pointers.append(var_data)
                        continue
                    # Case 2: normal response (reg, torch_type, data, grad)
                    else:
                        # if the response was send in a dict (vs list)
                        if isinstance(response, dict):
                            response = response.values()

                        registration, torch_type, var_data, var_grad = response

                        if registration is None:
                            pointers.append(var_data)
                        else:
                            pointer = hook_self._assemble_result_pointer(registration,
                                                                         torch_type,
                                                                         var_data,
                                                                         var_grad)
                            pointers.append(pointer)

                pointers = tuple(pointers) if len(pointers) > 1 else pointers[0]

                return pointers, has_remote, multiple_owners

        elif (has_remote and multiple_owners):
            raise NotImplementedError("""MPC not yet implemented:
                Torch objects need to be on the same machine in
                order to compute with them.""")
        # else:
        #     raise NotImplementedError("""SOMETHING WENT WRONG: This should be a local call""")

        return (None, has_remote, multiple_owners)

    @classmethod
    def _compile_command(cls, partial_func, has_self):
        """Assembles a JSON-serializable message from a partial function.

        Args:
        partial_func: a functools.partial or functools.partialmethod
            object wrapped around a torch command, its args, and its
            kwargs.
        has_self: a flag for whether or not the function is a method.
        """
        func = partial_func.func
        args = partial_func.args
        kwargs = partial_func.keywords
        command = {}
        command['has_self'] = has_self
        if has_self:
            command['self'] = args[0]
            args = args[1:]
        command['command'] = func.__name__
        command['args'] = args
        command['kwargs'] = kwargs

        encoder = utils.PythonEncoder()
        command, tensorvars = encoder.encode(command, retrieve_tensorvar=True)
        return command, tensorvars

    def _assemble_result_pointer(self, registration, torch_type, var_data, var_grad):
        """Assembles a pointer to a remote Torch object. Pointers feel like
        real Torch objects, but they're zero-dimensional until their
        contents are retrieved from their owners.

        Args
        registration (dict): registration attributes for the pointer
        torch_type: the torch class to construct the pointer from
        """
        # TODO: extend to iterables of tensor pointers

        try:
            torch_type = self.guard.types_guard(torch_type)
        except KeyError:
            raise TypeError(
                "Tried to receive a non-Torch object of type {}.".format(
                    torch_type))

        if var_data is not None:
            data = self._assemble_result_pointer(**var_data)

        elif torch_type in self.var_types:
            data = torch.Tensor(0)
        else:
            data = 0
        result = torch_type(data)
        # if var_grad is not None:
        # grad = self.assemble_result_pointer(**var_grad)
        # self.local_worker.register_object(
        # worker, result.grad, **var_grad['registration'])

        return self.local_worker.register_object(result, **registration)

    def _hook_torch_module(self):
        """Overloads functions in the main torch module.

        The way this is accomplished is by first moving all existing module functions in the torch
        module to old_<function_name_here>. Thus, the real :func:`torch.cat` will become
        :func:`torch.old_cat` and :func:`torch.cat` will have our hooking code. Generically,
        this hooking code checks to see if the tensor is on the current worker (aka, we can read it)
        or on a remote opne (and we only have a pointer). If the data is local, then the method
        will simply execute :func:`torch.old_cat`, but if the data for a tensor is remote, then
        it will instead send a message to the remote machine instructing it to perform an arbitrary
        command (:func:`torch.old_cat` on the remote machine).
        """

        for attr in self.torch_funcs:

            # Some functions we want to ignore (not override). Such functions have been hard coded
            # into the attribute self.torch_exclude
            if attr in self.torch_exclude:
                continue

            # if we haven't already overloaded this function
            if 'old_{}'.format(attr) in dir(torch):
                continue

            # if we haven't already overloaded this function (redundancy allowed)
            if 'old_' in attr:
                continue

            # Where the overloading happens
            lit = getattr(torch, attr)
            if (type(lit) in [types.FunctionType, types.BuiltinFunctionType]):
                passer = utils.pass_func_args(lit)
                new_attr = self._get_overload_function_in_torch_module(passer)
                setattr(torch, 'old_{}'.format(attr), lit)
                setattr(torch, attr, new_attr)

    def _hook_torch_functional(self):
        """Overloads functions in the torch.nn.functional

        The way this is accomplished is by first moving all existing module functions in the torch
        module to old_<function_name_here>. Thus, the real :func:`torch.nn.functional.relu` will become
        :func:`torch.nn.functional.old_cat` and :func:`torch.cat` will have our hooking code. Generically,
        this hooking code checks to see if the tensor is on the current worker (aka, we can read it)
        or on a remote one (and we only have a pointer). If the data is local, then the method
        will simply execute :func:`torch.old_cat`, but if the data for a tensor is remote, then
        it will instead send a message to the remote machine instructing it to perform an arbitrary
        command (:func:`torch.old_cat` on the remote machine).
        """

        for attr in self.torch_functional_funcs:

            # Some functions we want to ignore (not override). Such functions have been hard coded
            # into the attribute self.torch_exclude
            if attr in self.torch_functional_exclude:
                continue

            # if we haven't already overloaded this function
            if 'old_{}'.format(attr) in dir(torch.nn.functional):
                continue

            # if we haven't already overloaded this function (redundancy allowed)
            if 'old_' in attr:
                continue

            # Where the overloading happens
            lit = getattr(torch.nn.functional, attr)
            if (type(lit) in [types.FunctionType, types.BuiltinFunctionType]):
                passer = utils.pass_func_args(lit)
                new_attr = self._get_overload_function_in_torch_module(passer)
                setattr(torch.nn.functional, 'old_{}'.format(attr), lit)
                setattr(torch.nn.functional, attr, new_attr)



    # ######## END GENERIC method/function hooking logic #########
    # ######## BEGIN torch TENSOR hooking #########

    def _hook_tensor(self, tensor_type):
        """Overloading a given tensor_type"""
        # Overload 'special' methods here
        self._hook___new__(tensor_type)
        self._hook_tensor___repr__(tensor_type)
        self._hook_fixed_precision_methods(tensor_type)

        for attr in dir(tensor_type):
            # if we haven't already overloaded this function
            if 'old_{}'.format(attr) not in dir(tensor_type):
                # Conditions for inclusion/exclusion
                if attr in self.exclude:
                    continue
                lit = getattr(tensor_type, attr)
                is_base = attr in dir(object)
                is_desc = inspect.ismethoddescriptor(lit)
                is_func = isinstance(lit, types.FunctionType)
                try:
                    is_service_func = 'HookService' in lit.__qualname__
                except:
                    is_service_func = False
                is_old = re.match('old*', attr) is not None

                # Where the overloading happens
                if ((is_desc or (is_func and not is_service_func)) and not is_base and not is_old):
                    passer = utils.pass_method_args(lit)
                    new_attr = self._get_overload_method_in_tensor_or_var(passer)
                    setattr(tensor_type, 'old_{}'.format(attr), lit)
                    setattr(tensor_type, attr, new_attr)

        # Add in our own Grid-specific methods
        self._hook_send_(tensor_type)
        self._hook_get_(tensor_type)
        self._hook_tensor__serde(tensor_type)

    def _hook_tensor___del__(hook_self, tensor_type):
        """Overloads tensor_type.__del__"""
        def new____del__(self, *args):
            print("deleting tensor")

        tensor_type.__del__ = new____del__

    def _hook___new__(hook_self, tensorvar_type):
        """Overloads tensor_type.__new__ or Variale.__new__"""

        if ('old___new__' not in dir(tensorvar_type)):
            tensorvar_type.old___new__ = tensorvar_type.__new__

            def new___new__(cls, *args, **kwargs):
                result = cls.old___new__(cls, *args, **kwargs)
                result = hook_self.local_worker.register_object(
                    result, is_pointer=False)
                cls.fixed_precision = False
                return result

            tensorvar_type.__new__ = new___new__

    def _hook_tensor___repr__(hook_self, tensor_type):
        """Overload tensor_type.__repr__"""
        if ('old__repr__' not in dir(tensor_type)):
            tensor_type.old__repr__ = tensor_type.__repr__

            def new___repr__(self):
                if (not hasattr(self, 'owners')):
                    return self.old__repr__()
                _id_in_owners = hook_self.local_worker.id in self.owners
                if (hook_self.local_worker in self.owners or _id_in_owners):
                    return self.old__repr__()
                else:
                    return "[{}.{} - Locations:{}]".format(
                        tensor_type.__module__,
                        tensor_type.__name__,
                        self.owners)

            tensor_type.__repr__ = new___repr__

    def _hook_send_(hook_self, tensorvar_type):
        """Overloads the send methods"""
        def send_(self, workers, send_pointer=False):
            """Sends a Tensor or Variable object to a (sequence of) Grid workers.

            Args:
            workers: string (or sequence) containing IPFS address(es)
                of worker node(s).
            """

            # makes singleton, if needed
            workers = hook_self.local_worker._check_workers(self, workers)

            for worker in workers:
                hook_self.local_worker.send_obj(self,
                                                worker,
                                                send_pointer=send_pointer,
                                                delete_local=not send_pointer)
            if(not send_pointer):
                if(tensorvar_type == torch.autograd.variable.Variable):
                    zeroed = self
                else:
                    zeroed = self.old_set_(tensorvar_type(0))

                self = hook_self.local_worker.register_object(obj=zeroed,
                                                              id=self.id,
                                                              owners=workers,
                                                              is_pointer=True)
                if(tensorvar_type == torch.autograd.variable.Variable):
                    return hook_self._var_to_pointer(self)
                else:
                    return self
            else:
                return self

        setattr(tensorvar_type, 'send_', send_)
        setattr(tensorvar_type, 'send', send_)

    def _hook_get_(hook_self, torch_type):
        """Overloads the get methods"""
        def get_(self, reduce=lambda x: x[0]):
            """Gets a Torch object from its current owners.

            Args:
            reduce: (EXPERIMENTAL) How to reduce tensors that come from
                multiple workers
            """
            # TODO: fully generalize this to multiple workers; consider
            #       adding arguments for other tensor ids, e.g. mapping workers
            #       to tensors, and a reduce function (for example, would allow
            #       for built-in gradient averaging when Variable.get is done)
            #       (low priority)
            try:
                assert len(self.owners) == 1
            except AssertionError:
                raise NotImplementedError('Only able to get_ tensors belonging \
                                            to a single worker right now.')
            if hook_self.local_worker.id in self.owners:


                return self

            _out = hook_self.local_worker.request_obj(obj_id=self.id,
                                                      recipient=self.owners[0])
            x, request_obj_cleanup_method = _out

            hook_self.local_worker.register_object(x, id=x.id)

            _id = hook_self.local_worker.id  # for brevity
            if (type(self) != torch.autograd.variable.Variable and
                    type(self) != torch.nn.parameter.Parameter):
                _os = self.old_set_(x.type(self.type()))
            else:
                _os = self.old_set_(x.type(self.data.type()))  # for brevity
                self.data = x.data
                if (x.grad is not None):
                    self.grad = x.grad

            self = hook_self.local_worker.register_object(_os,
                                                          id=self.id,
                                                          owners=[_id])


            return self

        setattr(torch_type, 'get_', get_)

        # TODO: make this a non-inline version
        setattr(torch_type, 'get', get_)

    # ######## BEGIN torch VARIABLE hooking #########

    def _hook_variable(self):
        """Responsible for hooking Variable methods"""
        # Overload 'special' methods here
        self._hook___new__(torch.autograd.variable.Variable)
        self._hook_var_contents()
        self._hook_var_owners()

        for attr in dir(torch.autograd.variable.Variable):

            # Conditions for inclusion/exclusion
            if attr in self.exclude + self.var_exclude:
                continue
            lit = getattr(torch.autograd.variable.Variable, attr)
            is_base = attr in dir(object)
            is_desc = inspect.ismethoddescriptor(lit)
            # is_func = isinstance(type(lit), types.FunctionType)
            is_func = isinstance(lit, types.FunctionType)
            try:
                is_service_func = 'HookService' in lit.__qualname__
            except:
                is_service_func = False
            is_old = re.match('old*', attr) is not None

            # Where the overloading happens
            if ((is_desc or (is_func and not is_service_func)) and not is_base and not is_old):
                passer = utils.pass_method_args(lit)
                new_attr = self._get_overload_method_in_tensor_or_var(passer)
                setattr(torch.autograd.variable.Variable,
                        'old_{}'.format(attr), lit)
                setattr(torch.autograd.variable.Variable, attr, new_attr)

        self._hook_send_(torch.autograd.variable.Variable)
        self._hook_get_(torch.autograd.variable.Variable)
        self._hook_var_serde()

    def _hook_var_owners(hook_self):
        """Responsible for managing the 'owners' attribute"""
        @property
        def owners(self):
            if (hasattr(self, '_owners')):
                return self._owners
            else:
                hook_self.local_worker.register_object(obj=self)
                return self._owners

        @owners.setter
        def owners(self, value):
            self._owners = value

        torch.autograd.variable.Variable.owners = owners

    def _hook_var_contents(hook_self):
        """Overload Variable.data and Variable.grad properties."""
        torch.autograd.variable.Variable.old_data = torch.autograd.variable.Variable.data
        torch.autograd.variable.Variable.old_grad = torch.autograd.variable.Variable.grad

        hook_self._hook_new_data()
        hook_self._hook_new_grad()

    def _hook_new_data(hook_self):
        """Overloads new data attributes"""
        @property
        def new_data(self):
            if not hasattr(self, 'data_registered'):

                if (hasattr(self.old_data, 'id')):
                    obj_id = self.old_data.id
                else:
                    obj_id = None

                if (not hasattr(self, 'owners')):
                    self.owners = [hook_self.local_worker.id]

                self.old_data = hook_self.local_worker.register_object(obj=self.old_data,
                                                                       owners=self.owners,
                                                                       id=obj_id,
                                                                       is_pointer=self.is_pointer)
                self.data_registered = True

            return self.old_data

        @new_data.setter
        def new_data(self, new):
            self.old_data = new

        torch.autograd.variable.Variable.data = new_data

    def _hook_new_grad(hook_self):
        """Overloads new grad attributes"""
        @property
        def new_grad(self):
            if not hasattr(self, 'grad_registered'):

                if self.old_grad is not None:
                    self.grad_registered = True

                    # DO NOT REMOVE THIS LINE UNLESS YOU KNOW WHAT YOU'RE DOING
                    # for context behind this edit you can see the following video
                    # https://www.twitch.tv/videos/275838386
                    # long story short, we need to actually run the grad generating
                    # function (self.old_grad) and cache its value (the variable's
                    # gradient) in self.grad_backup so that python garbage collection
                    # doesn't delete the python object as a part of PyTorch's C++
                    # wrapping craziness (which does a lot of re-instantiating objects)
                    # In this case, re-instantiating the object gives it a new id because
                    # the object containing the old id goes away... this id is random which
                    # can create problems for PySyft

                    # also - keep this running ONLY within the if statement above that checks
                    # to see if self.grad_registered is not yet an attribute
                    self.grad_backup = self.old_grad
                    self.grad_backup.owners_backup = self.grad_backup.owners

                    self.grad.parent = self
                    self.grad_backup.parent = self

            return self.old_grad

        @new_grad.setter
        def new_grad(self, new):
            self.old_grad = new

        torch.autograd.variable.Variable.grad = new_grad

    def _hook_tensor__serde(hook_self, tensor_type):
        """Hooks object/json serialization and deserialization for tensor_type objects"""

        def ser(self, include_data=True):
            """Serializes a {} object to JSON.""".format(tensor_type)
            tensor_msg = {}
            tensor_msg['torch_type'] = self.type()
            if include_data:
                tensor_msg['data'] = self.tolist()
            tensor_msg['id'] = self.id
            if (type(self.owners[0]) is int):
                tensor_msg['owners'] = self.owners
            else:
                tensor_msg['owners'] = list(map(lambda x: x.id, self.owners))
            tensor_msg['is_pointer'] = not include_data

            return json.dumps(tensor_msg) + "\n"

        def deser(self, obj_msg):
            """Deserializes a {} object from JSON.""".format(tensor_type)

            # this could be a significant failure point, security-wise
            if('data' in obj_msg):
                data = hook_self.guard.tensor_contents_guard(obj_msg['data'])
                v = self(data)
            else:
                v = self([])
            return v

        tensor_type.ser = ser
        tensor_type.deser = deser

    def _hook_var_serde(hook_self):
        """Hooks object/json serialization and deserialization for Variable objects"""

        def ser(self, include_data=True):
            """Serializes a variable into a JSON object"""

            var_msg = {}
            var_msg['torch_type'] = re.search(
                "<class '(.*)'>", str(self.__class__)).group(1)
            var_msg['requires_grad'] = self.requires_grad
            var_msg['volatile'] = self.volatile
            var_msg['data'] = self.data.ser(include_data)
            if self.grad is not None:
                var_msg['grad'] = self.grad.ser(include_data)
            else:
                var_msg['grad'] = None
            var_msg['id'] = self.id
            if (type(self.owners[0]) is int):
                var_msg['owners'] = self.owners
            else:
                var_msg['owners'] = list(map(lambda x: x.id, self.owners))
            var_msg['is_pointer'] = not include_data
            return json.dumps(var_msg)

        def deser(self, obj_msg):
            """Deserializes a JSON object into a variable"""

            if 'data' in obj_msg.keys():
                data_msg = json.loads(obj_msg['data'])
                tensor_type = hook_self.guard.types_guard(data_msg['torch_type'])
                data_obj = tensor_type.deser(tensor_type, data_msg)
                # data_obj = hook_self.build_tensor(data_msg, tensor_type)
                data = hook_self.local_worker.handle_register(
                    data_obj, data_msg)

            if 'grad' in obj_msg.keys():
                if obj_msg['grad'] is not None:

                    grad_msg = json.loads(obj_msg['grad'])

                    var_type = hook_self.guard.types_guard(grad_msg['torch_type'])
                    grad_obj = hook_self._build_var(grad_msg, var_type)

                    grad = hook_self.local_worker.handle_register(grad_obj, grad_msg,
                                                                  force_attach_to_worker=False,
                                                                  temporary=True)
                else:
                    grad = None

            # nn.parameter.Parameter does not accept "volatile" as an input param.
            # https://pytorch.org/docs/0.3.1/autograd.html#variable
            if (self == torch.nn.parameter.Parameter):
                var = self(data, requires_grad=obj_msg['requires_grad'])
            else:
                var = self(data, volatile=obj_msg['volatile'],
                           requires_grad=obj_msg['requires_grad'])

            # var.grad = grad
            if (grad is not None):
                setattr(var, 'grad', grad)
            else:
                var.grad = None

            # this returns grad because garbage collection seems to do something really strange
            # if grad isn't returned here. It re-initializes the gradient somehow but in a way
            # where it's not registered (which is bad)
            return var

        torch.autograd.variable.Variable.ser = ser
        torch.autograd.variable.Variable.deser = deser

    def _var_to_pointer(self, var):
        """Overloads var to pointer function to enable 
        pointing to remote data
        """
        # recursively calls var_to_pointer in a depth first fashion
        # only recursive through variables (ignores .data)
        if var.grad is not None:
            self._var_to_pointer(var.grad)

        # deletes local data (because now it's a pointer to remote data)
        var.data.old_set_(var.data.__class__(0))

        # double check
        var.is_pointer = True

        return var

    def _build_var(self, obj_msg, torch_type):
        """Overloads variable building function"""
        if 'data' in obj_msg.keys():
            data_msg = json.loads(obj_msg['data'])
            tensor_type = self.guard.types_guard(data_msg['torch_type'])
            data_obj = tensor_type.deser(tensor_type, data_msg)
            # data_obj = self.build_tensor(data_msg, tensor_type)
            data = self.local_worker.handle_register(
                data_obj, data_msg, temporary=True)

        if 'grad' in obj_msg.keys():
            if obj_msg['grad'] is not None:
                grad_msg = json.loads(obj_msg['grad'])
                var_type = self.guard.types_guard(grad_msg['torch_type'])
                grad_obj = self._build_var(grad_msg, var_type)
                grad = self.local_worker.handle_register(
                    grad_obj, grad_msg, temporary=True)
            else:
                grad = None
        var = torch_type(data, volatile=obj_msg['volatile'],
                         requires_grad=obj_msg['requires_grad'])
        var.grad = grad
        return var

    # ######## END torch VARIABLE hooking #########
    # ######## BEGIN torch.nn.Module hooking #########

    def _hook_module(self):
        """Overloading for torch.nn.Module"""
        def module_is_missing_grad(model):
            """Overloads missing grad parameter in model"""
            missing_grad = False
            for p in model.parameters():
                if p.grad is None:
                    missing_grad = True
            return missing_grad

        def create_grad_objects(model):
            """Overloads create grad parameter for model"""
            for p in model.parameters():
                o = p.sum()
                o.backward()
                p.grad -= p.grad

        def module_send_(self, dest):
            """Overloads send to remote for torch.nn.Module"""
            if (module_is_missing_grad(self)):
                create_grad_objects(self)

            for p in self.parameters():
                p.send_(dest)

        torch.nn.Module.send_ = module_send_
        torch.nn.Module.send = module_send_

        def module_get_(self):
            """Overload get from remote for torch.nn.Module"""
            for p in self.parameters():
                p.get_()

        torch.nn.Module.get_ = module_get_
        torch.nn.Module.get = module_get_

    # ######## END torch.nn.Module hooking #########
