"""Parametric models."""
import abc
import collections.abc
from typing import Optional, Union

import torch.distributions
from torch import Tensor

from torchtree.core.abstractparameter import AbstractParameter
from torchtree.core.classproperty_decorator import classproperty
from torchtree.core.identifiable import Identifiable
from torchtree.core.parametric import ModelListener, ParameterListener, Parametric


class Model(Parametric, Identifiable, ModelListener, ParameterListener):
    """Parametric model.

    A Model can contain parameters and models and can monitor any
    changes. A Model is the building block of more complex models. This
    class is abstract.
    """

    _tag = None

    def __init__(self, id_: Optional[str]) -> None:
        Parametric.__init__(self)
        Identifiable.__init__(self, id_)
        self.listeners = []

    def add_model_listener(self, listener: ModelListener) -> None:
        self.listeners.append(listener)

    def remove_model_listener(self, listener: ModelListener) -> None:
        self.listeners.remove(listener)

    def add_parameter_listener(self, listener: ParameterListener) -> None:
        self.listeners.append(listener)

    def remove_parameter_listener(self, listener: ParameterListener) -> None:
        self.listeners.remove(listener)

    def fire_model_changed(self, obj=None, index=None) -> None:
        for listener in self.listeners:
            listener.handle_model_changed(self, obj, index)

    @classproperty
    def tag(cls) -> Optional[str]:
        return cls._tag

    def to(self, *args, **kwargs) -> None:
        """Performs Tensor dtype and/or device conversion using torch.to."""
        self._apply(lambda x: x.to(*args, **kwargs))

    def cuda(self, device: Optional[Union[int, torch.device]] = None) -> None:
        """Move tensors to CUDA using torch.cuda."""
        self._apply(lambda x: x.cuda(device))

    def cpu(self) -> None:
        """Move tensors to CPU memory using ~torch.cpu."""
        self._apply(lambda x: x.cpu())

    def _apply(self, fn):
        for param in self._parameters.values():
            fn(param)
        for model in self._models.values():
            fn(model)

    def models(self):
        """Returns sub-models."""
        for model in self._models.values():
            yield model

    @property
    def sample_shape(self) -> torch.Size:
        """Returns sample shape."""
        return self._sample_shape()

    @abc.abstractmethod
    def _sample_shape(self) -> torch.Size:
        """Implementation of sample_shape.

        :return: sample shape
        """
        ...


class CallableModel(Model, collections.abc.Callable):
    """Classes inheriting from :class:`Model` and
    :class:`collections.abc.Callable`.

    CallableModel are Callable and the returned value is cached in case
    we need to use this value multiple times without the need to
    recompute it.
    """

    def __init__(self, id_: Optional[str]) -> None:
        Model.__init__(self, id_)
        self.lp = None
        self.lp_needs_update = True

    @abc.abstractmethod
    def _call(self, *args, **kwargs) -> Tensor:
        pass

    def handle_parameter_changed(
        self, variable: AbstractParameter, index, event
    ) -> None:
        self.lp_needs_update = True
        self.fire_model_changed(self)

    def handle_model_changed(self, model, obj, index) -> None:
        self.lp_needs_update = True
        self.fire_model_changed(self)

    def __call__(self, *args, **kwargs) -> Tensor:
        if self.lp_needs_update:
            self.lp = self._call(*args, **kwargs)
            self.lp_needs_update = False
        return self.lp
