from abc import abstractmethod

from phylotorch.core.utils import process_object
from ..core.model import Model


class ClockModel(Model):
    def __init__(self, id_):
        super(ClockModel, self).__init__(id_)

    @abstractmethod
    def rates(self):
        pass


class SimpleClockModel(Model):

    def __init__(self, id_, rates, tree):
        self._rates = rates
        self.tree = tree
        self.add_parameter(rates)
        super(SimpleClockModel, self).__init__(id_)

    @property
    def rates(self):
        return self._rates.tensor

    def update(self, value):
        if isinstance(value, dict):
            if self._rates.id in value:
                self._rates.set_tensor(value[self._rates.id])
        else:
            self._rates = value

    def handle_model_changed(self, model, obj, index):
        pass

    def handle_parameter_changed(self, variable, index, event):
        self.fire_model_changed()

    @classmethod
    def from_json(cls, data, dic):
        id_ = data['id']
        tree = process_object(data['tree'], dic)
        rate = process_object(data['rate'], dic)
        return cls(id_, rate, tree)
