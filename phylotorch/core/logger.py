import csv
import json
import sys
from abc import abstractmethod
from typing import List, Union

import torch

from phylotorch.core.model import Parameter, CallableModel
from phylotorch.core.parameter_encoder import ParameterEncoder
from phylotorch.core.runnable import Runnable
from phylotorch.core.serializable import JSONSerializable
from phylotorch.core.utils import process_objects, process_object
from phylotorch.evolution.tree_model import TreeModel


class LoggerInterface(JSONSerializable, Runnable):
    """
    Interface for logging things like parameters or trees to a file.
    """

    @abstractmethod
    def log(self, *args, **kwargs) -> None:
        ...

    @abstractmethod
    def initialize(self) -> None:
        ...

    @abstractmethod
    def close(self) -> None:
        ...

    def run(self) -> None:
        self.initialize()
        self.log()
        self.close()


class Logger(LoggerInterface):
    r"""
    Class for logging Parameter objects to a file.

    :param objs: list of Parameter or CallableModel objects
    :type objs: list[Parameter or CallableModel]
    :param kwargs: optionals
    """

    def __init__(self, objs: List[Union[Parameter, CallableModel]], **kwargs) -> None:
        if 'file_name' in kwargs:
            self.file_name = kwargs['file_name']
            del kwargs['file_name']
        self.kwargs = kwargs
        self.objs = objs

        if self.file_name:
            self.f = open(self.file_name, 'w')
        else:
            self.f = sys.stdout
        self.writer = csv.writer(self.f, **self.kwargs)
        self.sample = 1

    def initialize(self) -> None:
        header = ['sample']
        for obj in self.objs:
            if isinstance(obj, Parameter):
                header.extend(['{}.{}'.format(obj.id, i) for i in range(obj.shape[-1])])

            else:
                header.append(obj.id)
        self.writer.writerow(header)

    def log(self, *args, **kwargs) -> None:
        row = [self.sample]
        for obj in self.objs:
            if isinstance(obj, Parameter):
                row.extend(obj.tensor.detach().numpy().tolist())
            else:
                row.append(obj().item())
        self.writer.writerow(row)
        self.sample += 1

    def close(self) -> None:
        if self.file_name is not None:
            self.f.close()

    @classmethod
    def from_json(cls, data, dic) -> 'Logger':
        r"""
        Create a Logger object.

        :param data: json representation of Logger object.
        :type data: dict[str,Any]
        :param dic: dictionary containing additional objects that can be referenced in data.
        :type dic: dict[str,Any]

        :return: a :class:`~phylotorch.core.logger.Logger` object.
        :rtype: Logger
        """
        params = process_objects(data['parameters'], dic)
        kwargs = {}
        for key in ('file_name', 'delimiter'):
            if key in data:
                kwargs[key] = data[key]
        return cls(params, **kwargs)


class TreeLogger(LoggerInterface):
    """
    Class for logging trees to a file.

    :param TreeModel objs: TreeModel object
    :param kwargs: optionals
    """

    def __init__(self, tree_model: TreeModel, **kwargs) -> None:
        self.tree_model = tree_model
        self.file_name = kwargs.get('file_name', None)
        self.kwargs = kwargs
        if self.file_name is not None:
            self.f = open(self.file_name, 'w')
        else:
            self.f = sys.stdout
        self.index = 1

    def initialize(self) -> None:
        if self.kwargs.get('format', 'newick') == 'nexus':
            self.f.write('#NEXUS\nBegin trees;\nTranslate\n')
            self.f.write(
                ',\n'.join([str(i + 1) + ' ' + x.replace("'", '') for i, x in enumerate(self.tree_model.taxa)]))
            self.f.write('\n;\n')

    def log(self, *args, **kwargs) -> None:
        format = self.kwargs.get('format', 'newick')
        if format == 'newick':
            self.tree_model.write_newick(self.f)
        else:
            self.f.write('tree {} = '.format(self.index))
            optionals = {'taxon_index': True}  # replace taxon name by its index
            self.tree_model.write_newick(self.f, **optionals)
        self.f.write('\n')
        self.index += 1

    def close(self) -> None:
        if self.kwargs.get('format', 'newick') == 'nexus':
            self.f.write('\nEND;')
        if self.file_name is not None:
            self.f.close()

    @classmethod
    def from_json(cls, data, dic) -> 'TreeLogger':
        r"""
        Create a TreeLogger object.

        :param data: json representation of TreeLogger object.
        :type data: dict[str,Any]
        :param dic: dictionary containing additional objects that can be referenced in data.
        :type dic: dict[str,Any]

        :return: a :class:`~phylotorch.core.logger.TreeLogger` object.
        :rtype: TreeLogger
        """
        tree = process_object(data['tree_model'], dic)
        kwargs = {}
        for key in ('file_name', 'format'):
            if key in data:
                kwargs[key] = data[key]
        return cls(tree, **kwargs)


class CSV(JSONSerializable, Runnable):
    r"""
    Class for writting parameters to a CSV file.

    :param objs: list of Parameter objects
    :type objs: list[Parameter]
    """

    def __init__(self, objs: List[Parameter], **kwargs) -> None:
        self.objs = objs
        self.file_name = kwargs.get('file_name', None)
        self.kwargs = kwargs

    def run(self) -> None:
        if self.file_name:
            f = open(self.file_name, 'w')
            writer = csv.writer(f, **self.kwargs)
        else:
            writer = csv.writer(sys.stdout, **self.kwargs)
        writer.writerow([obj.id for obj in self.objs])
        temp = torch.stack(list(map(lambda x: x.tensor, self.objs)))
        for i in range(temp.shape[1]):
            writer.writerow(temp[:, i].detach().numpy().tolist())
        if self.file_name:
            f.close()

    @classmethod
    def from_json(cls, data, dic) -> 'CSV':
        r"""
        Create a CSV object.

        :param data: json representation of CSV object.
        :type data: dict[str,Any]
        :param dic: dictionary containing additional objects that can be referenced in data.
        :type dic: dict[str,Any]

        :return: a :class:`~phylotorch.core.logger.CSV` object.
        :rtype: CSV
        """
        params = process_objects(data['parameters'], dic)
        kwargs = {}
        for key in ('file_name', 'delimiter'):
            if key in data:
                kwargs[key] = data[key]
        return cls(params, **kwargs)


class Dumper(JSONSerializable, Runnable):
    r"""
    Class for saving parameters to a json file.

    :param parameters: list of Parameters.
    :type parameters: list[Parameter]
    """

    def __init__(self, parameters: List[Parameter], **kwargs) -> None:
        if 'file_name' in kwargs:
            self.file_name = kwargs['file_name']
            del kwargs['file_name']
        self.kwargs = kwargs
        self.parameters = parameters

    def run(self) -> None:
        r"""
        Write the parameters to the file.
        """
        if self.file_name is not None:
            with open(self.file_name, 'w') as fp:
                json.dump(self.parameters, fp, cls=ParameterEncoder, **self.kwargs)
        else:
            json.dumps(self.parameters, cls=ParameterEncoder, **self.kwargs)

    @classmethod
    def from_json(cls, data, dic) -> 'Dumper':
        r"""
        Create a Dumper object.

        :param data: json representation of Dumper object.
        :type data: dict[str,Any]
        :param dic: dictionary containing additional objects that can be referenced in data.
        :type dic: dict[str,Any]

        :return: a :class:`~phylotorch.core.logger.Dumper` object.
        :rtype: Dumper
        """
        parameters = process_objects(data['parameters'], dic)
        kwargs = {'indent': 2}
        for key in ('file_name', 'indent'):
            if key in data:
                kwargs[key] = data[key]
        return cls(parameters, **kwargs)
