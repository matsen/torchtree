import abc
from abc import ABC
from io import StringIO
from typing import List, Optional, Union

import numpy as np
import torch
from dendropy import TaxonNamespace, Tree
from torch.distributions.transforms import Transform

from ..core.abstractparameter import AbstractParameter
from ..core.model import Model
from ..core.utils import JSONParseError, process_object
from ..typing import ID
from .taxa import Taxa


class GeneralNodeHeightTransform(Transform):
    r"""
    Transform from ratios to node heights.
    """
    bijective = True
    sign = +1

    def __init__(self, tree: 'TimeTreeModel', cache_size=0) -> None:
        super().__init__(cache_size=cache_size)
        self.tree = tree
        self.taxa_count = self.tree.taxa_count
        self.indices = (
            self.tree.preorder[np.argsort(self.tree.preorder[:, 1])].transpose()[
                0, self.taxa_count :
            ]
            - self.taxa_count
        )

    def _call(self, x: torch.Tensor) -> torch.Tensor:
        """Transform node ratios and root height to internal node heights."""
        heights = x.clone()
        for parent_id, id_ in self.tree.preorder:
            if id_ >= self.taxa_count:
                heights[..., id_ - self.taxa_count] = self.tree.bounds[id_] + x[
                    ..., id_ - self.taxa_count
                ] * (heights[..., parent_id - self.taxa_count] - self.tree.bounds[id_])
        return heights

    def _inverse(self, y: torch.Tensor) -> torch.Tensor:
        """Transform internal node heights to ratios/root height."""
        indices = self.tree.preorder[np.argsort(self.tree.preorder[:, 1])].transpose()
        bounds = self.tree.bounds[indices[1, self.taxa_count :]]
        return torch.cat(
            (
                (
                    y[
                        ...,
                        indices[1, self.taxa_count :] - self.taxa_count,
                    ]
                    - bounds
                )
                / (
                    y[
                        ...,
                        indices[0, self.taxa_count :] - self.taxa_count,
                    ]
                    - bounds
                ),
                y[..., -1:],
            )
        )

    def log_abs_det_jacobian(self, x, y):
        return torch.log(
            y[..., self.indices] - self.tree.bounds[self.taxa_count : -1]
        ).sum(-1)


def heights_to_branch_lengths(node_heights, bounds, indexing):
    taxa_count = int((bounds.shape[0] + 1) / 2)
    indices_sorted = indexing[np.argsort(indexing[:, 1])].transpose()
    return torch.cat(
        (
            node_heights[..., indices_sorted[0, :taxa_count] - taxa_count]
            - bounds[:taxa_count],
            node_heights[..., indices_sorted[0, taxa_count:] - taxa_count]
            - node_heights[..., indices_sorted[1, taxa_count:] - taxa_count],
        ),
        -1,
    )


def setup_indexes(tree):
    for node in tree.postorder_node_iter():
        node.index = -1
        node.annotations.add_bound_attribute("index")

    indexer = iter(range(len(tree.taxon_namespace), len(tree.taxon_namespace) * 2 - 1))
    taxa_dict = {taxon.label: idx for idx, taxon in enumerate(tree.taxon_namespace)}

    for node in tree.postorder_node_iter():
        if not node.is_leaf():
            node.index = next(indexer)
        else:
            node.index = taxa_dict[node.taxon.label]


def setup_dates(tree, heterochronous=False):
    # parse dates
    if heterochronous:
        dates = {}
        for node in tree.leaf_node_iter():
            dates[str(node.taxon)] = float(str(node.taxon).rsplit('_', 1)[:-1])

        max_date = max(dates.values())
        min_date = min(dates.values())
        print(min_date, max_date)

        # time starts at 0
        if min_date == 0:
            for node in tree.leaf_node_iter():
                node.date = dates[str(node.taxon)]
                node.original_date = dates[str(node.taxon)]
            oldest = max_date
        # time is a year
        else:
            for node in tree.leaf_node_iter():
                node.date = max_date - dates[str(node.taxon)]
                node.original_date = dates[str(node.taxon)]
            oldest = max_date - min_date
    else:
        for node in tree.postorder_node_iter():
            node.date = 0.0
            node.original_date = 0.0
        oldest = None

    return oldest


def initialize_dates_from_taxa(tree, taxa, tag='date'):
    dates = [taxon[tag] for taxon in taxa]
    max_date = max(dates)

    # parse dates
    if max_date != 0.0:
        # time starts at 0
        if min(dates) == 0.0:
            for node in tree.leaf_node_iter():
                node.date = taxa[node.index][tag]
                node.original_date = node.date
        # time is a year
        else:
            for node in tree.leaf_node_iter():
                node.date = max_date - taxa[node.index][tag]
                node.original_date = taxa[node.index][tag]
    else:
        for node in tree.leaf_node_iter():
            node.date = 0.0
            node.original_date = 0.0


def heights_from_branch_lengths(tree):
    heights = np.empty(2 * len(tree.taxon_namespace) - 1)
    for node in tree.postorder_node_iter():
        if node.is_leaf():
            heights[node.index] = node.date
        else:
            child = next(node.child_node_iter())
            heights[node.index] = heights[child.index] + float(child.edge_length)
    return heights[len(tree.taxon_namespace) :]


def parse_tree(taxa, data):
    taxon_namespace = TaxonNamespace([taxon.id for taxon in taxa])
    taxon_namespace_size = len(taxon_namespace)
    if 'newick' in data:
        tree = Tree.get(
            data=data['newick'],
            schema='newick',
            preserve_underscores=True,
            rooting='force-rooted',
            taxon_namespace=taxon_namespace,
        )
    elif 'file' in data:
        tree = Tree.get(
            path=data['file'],
            schema='newick',
            preserve_underscores=True,
            rooting='force-rooted',
            taxon_namespace=taxon_namespace,
        )
    else:
        raise ValueError('Tree model requires a file or newick element to be specified')
    if taxon_namespace_size != len(taxon_namespace):
        raise ValueError(
            'Some taxon names in the tree do not match those in the Taxa object'
        )
    tree.resolve_polytomies(update_bipartitions=True)
    setup_indexes(tree)
    return tree


class TreeModel(Model):
    _tag = 'tree_model'

    @abc.abstractmethod
    def branch_lengths(self) -> torch.Tensor:
        ...

    @property
    @abc.abstractmethod
    def postorder(self) -> List[List[int]]:
        ...

    @property
    @abc.abstractmethod
    def taxa(self) -> List[str]:
        ...

    @abc.abstractmethod
    def write_newick(self, steam, **kwargs) -> None:
        ...


class AbstractTreeModel(TreeModel, ABC):
    def __init__(self, id_: ID, tree, taxa: Taxa) -> None:
        TreeModel.__init__(self, id_)
        self.tree = tree
        self._taxa = taxa
        self.taxa_count = len(tree.taxon_namespace)
        self._postorder = []
        self.update_traversals()

    def update_traversals(self) -> None:
        # postorder for peeling
        self._postorder = []
        for node in self.tree.postorder_node_iter():
            if not node.is_leaf():
                children = node.child_nodes()
                self._postorder.append(
                    (node.index, children[0].index, children[1].index)
                )

    def handle_model_changed(self, model, obj, index):
        pass

    @property
    def postorder(self):
        return self._postorder

    @property
    def taxa(self):
        # return [taxon.label for taxon in self.tree.taxon_namespace]
        return [taxon.id for taxon in self._taxa]

    def as_newick(self, **kwargs):
        out = StringIO()
        self.write_newick(out, **kwargs)
        return out.getvalue()

    def write_newick(self, steam, **kwargs) -> None:
        self._write_newick(self.tree.seed_node, steam, **kwargs)

    def _write_newick(self, node, steam, **kwargs) -> None:
        if not node.is_leaf():
            steam.write('(')
            for i, child in enumerate(node.child_node_iter()):
                self._write_newick(child, steam, **kwargs)
                if i == 0:
                    steam.write(',')
            steam.write(')')
        else:
            taxon_index = kwargs.get('taxon_index', None)
            if not taxon_index:
                steam.write(str(node.taxon).strip("'"))
            else:
                steam.write(str(node.index + 1))
        if node.parent_node is not None:
            branch_lengths = kwargs.get('branch_lengths', self.branch_lengths())
            steam.write(':{}'.format(branch_lengths[node.index]))
        else:
            steam.write(';')


class UnRootedTreeModel(AbstractTreeModel):
    def __init__(
        self, id_: ID, tree, taxa: Taxa, branch_lengths: AbstractParameter
    ) -> None:
        super().__init__(id_, tree, taxa)
        self._branch_lengths = branch_lengths

    def branch_lengths(self) -> torch.Tensor:
        return self._branch_lengths.tensor

    @property
    def sample_shape(self) -> torch.Size:
        return self._branch_lengths.tensor.shape[:-1]

    def handle_parameter_changed(self, variable, index, event):
        self.fire_model_changed()

    @staticmethod
    def json_factory(
        id_: str,
        newick: str,
        branch_lengths: Union[dict, list, str],
        taxa: Union[dict, list, str],
        **kwargs
    ):
        r"""
        Factory for creating tree models in JSON format.

        :param id_: ID of the tree model
        :param newick: tree in newick format
        :param branch_lengths: branch lengths
        :param taxa: list dictionary of taxa with attributes or str reference


        :key branch_lengths_id:  ID of branch_lengths (default: branch_lengths)
        :key taxa_id:  ID of taxa (default: taxa)
        :key keep_branch_lengths: if True use branch lengths in newick tree

        :return: tree model in JSON format compatible with from_json class method
        """

        tree_model = {
            'id': id_,
            'type': 'phylotorch.evolution.tree_model.UnRootedTreeModel',
            'newick': newick,
        }
        if 'keep_branch_lengths' in kwargs and kwargs['keep_branch_lengths']:
            tree_model['keep_branch_lengths'] = kwargs['keep_branch_lengths']

        if isinstance(branch_lengths, list):
            tree_model['branch_lengths'] = {
                "id": kwargs.get('branch_lengths_id', 'branch_lengths'),
                "type": "phylotorch.Parameter",
                "tensor": branch_lengths,
            }
        elif isinstance(branch_lengths, (dict, str)):
            tree_model['branch_lengths'] = branch_lengths

        if isinstance(taxa, dict):
            taxon_list = []
            for taxon in taxa.keys():
                taxon_list.append(
                    {"id": taxon, "type": "phylotorch.evolution.taxa.Taxon"}
                )
            tree_model['taxa'] = {
                'id': kwargs.get('taxa_id', 'taxa'),
                'type': 'phylotorch.evolution.taxa.Taxa',
                'taxa': taxon_list,
            }
        elif isinstance(taxa, list):
            tree_model['taxa'] = {
                'id': kwargs.get('taxa_id', 'taxa'),
                'type': 'phylotorch.evolution.taxa.Taxa',
                'taxa': taxa,
            }
        else:
            tree_model['taxa'] = taxa

        return tree_model

    @classmethod
    def from_json(cls, data, dic):
        id_ = data['id']
        taxa = process_object(data['taxa'], dic)
        tree = parse_tree(taxa, data)
        branch_lengths = process_object(data['branch_lengths'], dic)
        if 'keep_branch_lengths' in data:
            blens = [
                float(node.edge_length)
                for node in sorted(
                    list(
                        tree.postorder_node_iter(
                            lambda node: node.parent_node is not None
                        )
                    ),
                    key=lambda x: x.index,
                )
            ]
            child_1, child_2 = tree.seed_node.child_node_iter()
            blens[child_1.index] += child_2.edge_length
            blens[child_2.index] += child_1.edge_length

            branch_lengths.tensor = torch.tensor(blens[:-1], dtype=branch_lengths.dtype)
        return cls(id_, tree, taxa, branch_lengths)


class TimeTreeModel(AbstractTreeModel):
    def __init__(
        self, id_: ID, tree, taxa: Taxa, internal_heights: AbstractParameter
    ) -> None:
        super().__init__(id_, tree, taxa)
        self._internal_heights = internal_heights
        self.taxa_count = len(tree.taxon_namespace)
        self.bounds = None
        self.sampling_times = None
        self.update_leaf_heights()
        self.update_bounds()
        self._branch_lengths = None  # tensor
        self._node_heights = None  # tensor
        self.branch_lengths_need_update = True
        self.heights_need_update = True

    def update_leaf_heights(self) -> None:
        leaf_heights = [None] * len(self._taxa)

        dates = [taxon['date'] for taxon in self._taxa]
        max_date = max(dates)

        # time starts at 0
        if min(dates) == 0.0:
            for idx, taxon in enumerate(self._taxa):
                leaf_heights[idx] = taxon['date']
        # time is a year
        else:
            for idx, taxon in enumerate(self._taxa):
                leaf_heights[idx] = max_date - taxon['date']

        self.sampling_times = torch.tensor(leaf_heights)

    def update_bounds(self) -> None:
        taxa_count = self.taxa_count
        internal_heights = [None] * (taxa_count - 1)
        for node, left, right in self.postorder:
            left_height = (
                self.sampling_times[left]
                if left < taxa_count
                else internal_heights[left - taxa_count]
            )
            right_height = (
                self.sampling_times[right]
                if right < taxa_count
                else internal_heights[right - taxa_count]
            )

            internal_heights[node - taxa_count] = (
                left_height if left_height > right_height else right_height
            )
        self.bounds = torch.cat(
            (self.sampling_times, torch.stack(internal_heights)), -1
        )

    def update_traversals(self):
        super().update_traversals()
        # preoder indexing to go from ratios to heights
        self.preorder = np.array(
            [
                (node.parent_node.index, node.index)
                for node in self.tree.preorder_node_iter()
                if node != self.tree.seed_node
            ]
        )

    @property
    def node_heights(self) -> torch.Tensor:
        if self.heights_need_update:
            self._node_heights = torch.cat(
                (
                    self.sampling_times.expand(
                        self._internal_heights.tensor.shape[:-1] + (-1,)
                    ),
                    self._internal_heights.tensor,
                ),
                -1,
            )
            self.heights_need_update = False
        return self._node_heights

    def branch_lengths(self) -> torch.Tensor:
        """Return branch lengths calculated from node heights.

        Branch lengths are indexed by node index on the distal side of
        the tree. For example branch_lengths[0] corresponds to the branch
        starting from taxon with index 0.

        :return: branch lengths of tree
        :rtype: torch.Tensor
        """
        if self.branch_lengths_need_update:
            indices_sorted = self.preorder[np.argsort(self.preorder[:, 1])].transpose()
            heights = self.node_heights
            self._branch_lengths = (
                heights[..., indices_sorted[0]] - heights[..., indices_sorted[1]]
            )
            self.branch_lengths_need_update = False
        return self._branch_lengths

    def handle_parameter_changed(self, variable, index, event):
        self.branch_lengths_need_update = True
        self.heights_need_update = True
        self.fire_model_changed()

    @property
    def sample_shape(self) -> torch.Size:
        return self._internal_heights.tensor.shape[:-1]

    def cuda(self, device: Optional[Union[int, torch.device]] = None) -> None:
        super().cuda(device)
        self.bounds.cuda(device)

    def cpu(self) -> None:
        super().cpu()
        self.bounds.cpu()

    @staticmethod
    def json_factory(
        id_: str,
        newick: str,
        internal_heights: Union[dict, list, str],
        taxa: Union[dict, list, str],
        **kwargs
    ):
        r"""
        Factory for creating tree models in JSON format.

        :param id_: ID of the tree model
        :param newick: tree in newick format
        :param taxa: dictionary of taxa with attributes or str reference


        :key internal_heights_id:  ID of internal_heights
        :key internal_heights: internal node heights. Can be a list of floats,
        a dictionary corresponding to a transformed parameter, or a str corresponding
        to a reference

        :return: tree model in JSON format compatible with from_json class method
        """

        tree_model = {
            'id': id_,
            'type': 'phylotorch.evolution.tree_model.TimeTreeModel',
            'newick': newick,
        }
        if 'keep_branch_lengths' in kwargs and kwargs['keep_branch_lengths']:
            tree_model['keep_branch_lengths'] = kwargs['keep_branch_lengths']

        node_heights_id = kwargs.get('internal_heights_id', None)
        if isinstance(internal_heights, list):
            tree_model['internal_heights'] = {
                "id": node_heights_id,
                "type": "phylotorch.Parameter",
                "tensor": internal_heights,
            }
        elif isinstance(internal_heights, (dict, str)):
            tree_model['internal_heights'] = internal_heights

        if isinstance(taxa, dict):
            taxon_list = []
            for taxon in taxa.keys():
                taxon_list.append(
                    {
                        "id": taxon,
                        "type": "phylotorch.evolution.taxa.Taxon",
                        "attributes": {"date": taxa[taxon]},
                    }
                )
            tree_model['taxa'] = {
                'id': kwargs.get('taxa_id', 'taxa'),
                'type': 'phylotorch.evolution.taxa.Taxa',
                'taxa': taxon_list,
            }
        elif isinstance(taxa, list):
            tree_model['taxa'] = {
                'id': kwargs.get('taxa_id', 'taxa'),
                'type': 'phylotorch.evolution.taxa.Taxa',
                'taxa': taxa,
            }
        else:
            tree_model['taxa'] = taxa

        return tree_model

    @classmethod
    def from_json(cls, data, dic):
        id_ = data['id']
        taxa = process_object(data['taxa'], dic)
        tree = parse_tree(taxa, data)
        initialize_dates_from_taxa(tree, taxa)

        # TODO: tree_model and internal_heights may have circular references to each
        #       other when internal_heights is a transformed Parameter requiring
        #       the tree_model
        if id_ in dic:
            raise JSONParseError('Object with ID `{}\' already exists'.format(id_))
        tree_model = cls(id_, tree, taxa, None)
        dic[id_] = tree_model
        tree_model._internal_heights = process_object(data['internal_heights'], dic)

        if data.get('keep_branch_lengths', False):
            tree_model._internal_heights.tensor = torch.tensor(
                heights_from_branch_lengths(tree),
                dtype=tree_model._internal_heights.dtype,
            )

        return tree_model


#
# class ReparameterizedTimeTreeModel(TimeTreeModel, CallableModel):
#     def __init__(
#         self, id_: ID, tree, taxa: Taxa, ratios_root_heights: AbstractParameter
#     ) -> None:
#         CallableModel.__init__(self, id_)
#         TimeTreeModel.__init__(self, id_, tree, taxa, ratios_root_heights)
#         # self.bounds = None
#         # self.update_bounds()
#         self._heights = None
#         self.transform = GeneralNodeHeightTransform(self)
#
#     # def update_bounds(self) -> None:
#     #     taxa_count = self.taxa_count
#     #     internal_heights = [None] * (taxa_count - 1)
#     #     for node, left, right in self.postorder:
#     #         left_height = (
#     #             self.sampling_times[left]
#     #             if left < taxa_count
#     #             else internal_heights[left - taxa_count]
#     #         )
#     #         right_height = (
#     #             self.sampling_times[right]
#     #             if right < taxa_count
#     #             else internal_heights[right - taxa_count]
#     #         )
#     #
#     #         internal_heights[node - taxa_count] = (
#     #             left_height if left_height > right_height else right_height
#     #         )
#     #     self.bounds = torch.cat(
#     #         (self.sampling_times, torch.stack(internal_heights)), -1
#     #     )
#
#     def update_node_heights(self) -> None:
#         self._heights = self.transform(self._internal_heights.tensor)
#         self._node_heights = torch.cat(
#             (
#                 self.sampling_times.expand(
#                     self._internal_heights.tensor.shape[:-1] + (-1,)
#                 ),
#                 self._heights,
#             ),
#             -1,
#         )
#
#     @property
#     def node_heights(self) -> torch.Tensor:
#         if self.heights_need_update:
#             self.update_node_heights()
#             self.heights_need_update = False
#         return self._node_heights
#
#     # def update_traversals(self):
#     #     super().update_traversals()
#     #     # preoder indexing to go from ratios to heights
#     #     self.preorder = np.array(
#     #         [
#     #             (node.parent_node.index, node.index)
#     #             for node in self.tree.preorder_node_iter()
#     #             if node != self.tree.seed_node
#     #         ]
#     #     )
#
#     def _call(self, *args, **kwargs) -> Tensor:
#         if self.heights_need_update:
#             self.update_node_heights()
#         return self.transform.log_abs_det_jacobian(
#             self._internal_heights.tensor, self._heights
#         )
#
#     @classmethod
#     def from_json(cls, data, dic):
#         id_ = data['id']
#         taxa = process_object(data['taxa'], dic)
#         tree = parse_tree(taxa, data)
#         initialize_dates_from_taxa(tree, taxa)
#
#         root_height = process_object(data['root_height'], dic)
#         ratios = process_object(data['ratios'], dic)
#         ratios_root_height = CatParameter(None, [ratios, root_height], dim=-1)
#
#         if data.get('keep_branch_lengths', False):
#             ratios_root_height.tensor = torch.tensor(
#                 heights_from_branch_lengths(tree),
#                 dtype=ratios_root_height.dtype,
#             )
#         return cls(id_, tree, taxa, ratios_root_height)
