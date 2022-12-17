# Copyright 2018 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the 'License');
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an 'AS IS' BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ======================================
"""Experimental support for defining XLA shardings."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import numpy as _np  # Avoids becoming a part of public Tensorflow API.

from tensorflow.compiler.tf2xla.python import xla as tf2xla
from tensorflow.compiler.xla import xla_data_pb2
from tensorflow.core.framework import attr_value_pb2


class Sharding(object):
  """A class to support adding sharding attributes to Ops.

  Use the factory constructors and then call apply_to_tensor:
    Sharding.replicate().apply_to_tensor(tensor)
  """

  def __init__(self, proto=None):
    """Do not use this constructor; use the factory functions below."""
    self._proto = proto

  @classmethod
  def replicate(cls):
    """Returns a replicated sharding attribute.

    This causes an op to be computed in its entirety independently on all
    cores in the XLA device.
    """
    return Sharding(
        proto=xla_data_pb2.OpSharding(type=xla_data_pb2.OpSharding.REPLICATED))

  @classmethod
  def manual(cls):
    """Returns a manuall sharding attribute.

    This means the op is manually partitioned by the user and XLA will not
    change the shapes.
    """
    return Sharding(
        proto=xla_data_pb2.OpSharding(type=xla_data_pb2.OpSharding.MANUAL))

  @classmethod
  def assign_device(cls, core):
    """Returns an AssignDevice sharding attribute.

    This causes an op to be computed in its entirety only on one core in
    the XLA device.
    Args:
      core: The core to assign this Op to.
    """
    return Sharding(
        proto=xla_data_pb2.OpSharding(
            type=xla_data_pb2.OpSharding.MAXIMAL,
            tile_assignment_dimensions=[1],
            tile_assignment_devices=[core]))

  @classmethod
  def tile(cls, tile_assignment):
    """Returns a Tiled sharding attribute.

    This causes an op to be partially computed on multiple cores in the
    XLA device.

    Args:
      tile_assignment: An np.ndarray describing the topology of the tiling and
        which device will compute which part of the topology.

    Raises:
      TypeError: tile_assignment was not of np.array type.

    TODO(jmolloy): This concept is nefarious and is not
    something we really want to expose to users (especially as the
    contract for tile_assignment is very strict).
    """
    if not isinstance(tile_assignment, _np.ndarray):
      raise TypeError('Tile assignment must be of type np.ndarray')
    dims = list(tile_assignment.shape)
    flattened_devices = tile_assignment.reshape(-1, order='C')
    return Sharding(
        proto=xla_data_pb2.OpSharding(
            type=xla_data_pb2.OpSharding.OTHER,
            tile_assignment_dimensions=dims,
            tile_assignment_devices=list(flattened_devices)))

  @classmethod
  def partial_tile(cls, tile_assignment):
    """Returns a partially tiled sharding attribute.

    This is similar to tile(), but tile_assignment has one more dimension than
    the tensor, and tiles in the last dimension of tile_assignment are
    replicated.

    Args:
      tile_assignment: An np.ndarray describing the topology of the tiling and
        which device will compute which part of the topology.

    Raises:
      TypeError: tile_assignment was not of np.array type.
    """
    if not isinstance(tile_assignment, _np.ndarray):
      raise TypeError('PartialTile assignment must be of type np.ndarray')
    dims = list(tile_assignment.shape)
    flattened_devices = tile_assignment.reshape(-1, order='C')
    return Sharding(
        proto=xla_data_pb2.OpSharding(
            type=xla_data_pb2.OpSharding.OTHER,
            tile_assignment_dimensions=dims,
            tile_assignment_devices=list(flattened_devices),
            replicate_on_last_tile_dim=True))

  @classmethod
  def split(cls, tensor, split_dimension, num_devices, input_shape=None):
    """Returns a Sharding that splits a tensor across a dimension.

    This creates a Tiled attribute, similar to tile(), but easier to use for the
    common case of tiling a tensor N ways in one dimension.

    Args:
      tensor: A tf.Tensor to split.
      split_dimension: The dimension number to split.
      num_devices: The number of cores to split `tensor` over.
      input_shape: The shape of the original tensor.

    Raises:
      ValueError: The tensor to split was smaller in the split dimension than
        the number of devices to split over.
    """
    shape = input_shape or tensor.shape.as_list()
    if (shape[split_dimension] is not None and
        shape[split_dimension] < num_devices):
      raise ValueError('Split dimension was smaller than the required number '
                       'of splits: shape=%r, dimension=%r, num_devices=%r' %
                       (shape, split_dimension, num_devices))

    tile_assignment_dims = [1] * len(shape)
    tile_assignment_dims[split_dimension] = num_devices

    return Sharding(
        proto=xla_data_pb2.OpSharding(
            type=xla_data_pb2.OpSharding.OTHER,
            tile_assignment_dimensions=tile_assignment_dims,
            tile_assignment_devices=range(num_devices)))

  def apply_to_tensor(self,
                      tensor,
                      assign_tuple_sharding=False,
                      use_sharding_op=False):
    """Applies this Sharding attribute to `tensor`.

    Args:
      tensor: A tf.Tensor to split.
      assign_tuple_sharding: If the sharding type should be a tuple.
      use_sharding_op: whether to create a sharding op on `tensor`.

    Returns:
      The tensor with Sharding attribute.
    """
    proto = self._proto
    if use_sharding_op:
      if assign_tuple_sharding:
        proto = self._create_tuple_proto(num_outputs=1)
      tensor = tf2xla.sharding(tensor, sharding=proto.SerializeToString())
    elif assign_tuple_sharding or len(tensor.op.outputs) > 1:
      proto = self._get_or_create_tuple_proto(tensor.op)
      # We can't mutate an element of old_proto.tuple_shardings, so create
      # a new proto.
      tuple_shardings = list(proto.tuple_shardings)
      tuple_shardings[tensor.value_index] = self._proto
      proto = xla_data_pb2.OpSharding(
          type=xla_data_pb2.OpSharding.TUPLE, tuple_shardings=tuple_shardings)

    # TODO(jmolloy): This need to be seriously revisited before declaring this
    # API available for public use.
    # pylint: disable=protected-access
    tensor.op._set_attr('_XlaSharding',
                        attr_value_pb2.AttrValue(s=proto.SerializeToString()))
    return tensor

  def apply_to_operation(self, operation):
    """Applies this Sharding attribute to `operation`.

    Args:
      operation: A tf.Operation to add sharding annotation.
    """
    attr_value = attr_value_pb2.AttrValue(s=self._proto.SerializeToString())
    # pylint: disable=protected-access
    operation._set_attr('_XlaSharding', attr_value)

  @property
  def proto(self):
    """Return the sharding protobuf of type xla_data_pb2.OpSharding."""
    return self._proto

  def _get_or_create_tuple_proto(self, op):
    try:
      attr = op.get_attr('_XlaSharding')
      proto = xla_data_pb2.OpSharding()
      proto.ParseFromString(attr)
      return proto
    except ValueError:
      return self._create_tuple_proto(len(op.outputs))

  def _create_tuple_proto(self, num_outputs):
    shardings = [
        xla_data_pb2.OpSharding(type=xla_data_pb2.OpSharding.REPLICATED)
    ] * num_outputs
    return xla_data_pb2.OpSharding(
        type=xla_data_pb2.OpSharding.TUPLE, tuple_shardings=shardings)


def copy_sharding(from_tensor, to_tensor, use_sharding_op=False):
  """Copies the a tensor's sharding to another.

  Args:
    from_tensor: Source tensor. Must be the sole output of an op.
    to_tensor: the tensor the annotate with the copy.
    use_sharding_op: whether to create a sharding op on `to_tensor`.

  Returns:
    A tensor with sharding annotation copied from `from_tensor`.
  """
  sharding = get_tensor_sharding(from_tensor)
  if sharding is None:
    return to_tensor

  if use_sharding_op:
    to_tensor = tf2xla.sharding(to_tensor, sharding=sharding)
  attr_value = attr_value_pb2.AttrValue(s=sharding)
  # pylint: disable=protected-access
  to_tensor.op._set_attr('_XlaSharding', attr_value)
  return to_tensor

# Helpers for the above factory functions that allow easy application of
# shardings, for example:
#   tensor = xla_sharding.replicate(tensor)


def replicate(tensor, assign_tuple_sharding=False, use_sharding_op=False):
  return Sharding.replicate().apply_to_tensor(
      tensor,
      assign_tuple_sharding=assign_tuple_sharding,
      use_sharding_op=use_sharding_op)


def assign_device(tensor,
                  device,
                  assign_tuple_sharding=False,
                  use_sharding_op=False):
  """Returns a tensor that has AssignDevice sharding attribute."""
  return Sharding.assign_device(device).apply_to_tensor(
      tensor,
      assign_tuple_sharding=assign_tuple_sharding,
      use_sharding_op=use_sharding_op)


def tile(tensor,
         tile_assignment,
         assign_tuple_sharding=False,
         use_sharding_op=False):
  """Returns a tensor that has tiled sharding.

  Args:
    tensor: A tf.Tensor to shard.
    tile_assignment: An np.ndarray describing the topology of the tiling and
      which device will compute which part of the topology.
    assign_tuple_sharding: If the sharding type should be a tuple.
    use_sharding_op: If true, adds a sharding op to set the sharding.
  """
  return Sharding.tile(tile_assignment).apply_to_tensor(
      tensor,
      assign_tuple_sharding=assign_tuple_sharding,
      use_sharding_op=use_sharding_op)


def split(tensor,
          split_dimension,
          num_devices,
          assign_tuple_sharding=False,
          use_sharding_op=False,
          input_shape=None):
  """Returns a tensor that is split along the given dimension.

  Args:
    tensor: A tf.Tensor to split.
    split_dimension: The dimension to split.
    num_devices: The number of devices to partition the dimension.
    assign_tuple_sharding: If the sharding type should be a tuple.
    use_sharding_op: If true, adds a sharding op to set the sharding.
    input_shape: The full shape of the input tensor.
  """
  return Sharding.split(tensor, split_dimension, num_devices,
                        input_shape).apply_to_tensor(
                            tensor,
                            assign_tuple_sharding=assign_tuple_sharding,
                            use_sharding_op=use_sharding_op)


def partial_tile(tensor, tile_assignment, use_sharding_op=False):
  """Returns a tensor that has tiled sharding.

  Args:
    tensor: A tf.Tensor to shard.
    tile_assignment: An np.ndarray describing the topology of the tiling and
      which device will compute which part of the topology. It must have one
      more dimension than tensor, and the last dimension represents partially
      replicated tiles.
    use_sharding_op: If true, adds a sharding op to set the sharding.
  """
  return Sharding.partial_tile(tile_assignment).apply_to_tensor(
      tensor, use_sharding_op=use_sharding_op)


def get_op_sharding(op):
  """Returns sharding attribute of an op.

  Args:
    op: a TensorFlow op.

  Returns:
    The attribute representing XLA sharding on this op.
  """
  try:
    return op.get_attr('_XlaSharding')
  except (ValueError, AttributeError):
    return None


def get_tensor_sharding(tensor):
  """Returns sharding attribute of a Tensor.

  Args:
    tensor: a Tensor.

  Returns:
    The attribute representing XLA sharding on tensor's op.
  """
  try:
    return get_op_sharding(tensor.op)
  except AttributeError:
    # AttributeError: Tensor.op is meaningless when eager execution is enabled.
    return None


def get_sharding_tile_shape(sharding):
  """Returns the tile assignment shape for a sharded Tensor.

  Args:
    sharding: a serialized OpSharding message describing the layout of a
      sharded Tensor.

  Returns:
    A list, for each dimension of the sharded Tensor, of the number of shards
      into which it has been split. Returns None if the input indicates no tile
      assignments.
  """
  if sharding is None:
    return None
  sharding_message = xla_data_pb2.OpSharding()
  sharding_message.ParseFromString(sharding)
  return sharding_message.tile_assignment_dimensions or None


def auto_to_manual_spmd_partition(tensor, manual_sharding):
  """Switches from automatic SPMD partitioning to manual partitioning.

  Converts a full-shaped tensor (to be automatically partitioned by SPMD
  partitioner) to a shard-shaped tensor to be consumed by manually partitioned
  ops.

  Args:
    tensor: A tf.Tensor in full shape.
    manual_sharding: a serialized string of OpSharding to be used in manual
      partitioning.

  Returns:
    A shard-shaped tensor to be consumed by manually partitioned ops.
  """
  return tf2xla.spmd_full_to_shard_shape(
      tensor, manual_sharding=manual_sharding)


def manual_to_auto_spmd_partition(tensor, manual_sharding, full_shape):
  """Switches from manual partitioning to automatic SPMD partitioning.

  Converts a shard-shaped tensor (manually partitioned in SPMD-style) to a
  full-shaped tensor to be partitioned automatically by the SPMD partitioner.

  Args:
    tensor: A tf.Tensor in shard shape.
    manual_sharding: a serialized string of OpSharding to be used in manual
      partitioning.
    full_shape: the shape of tensor before partitioning.

  Returns:
    A full-shaped tensor to be partitioned automatically by the SPMD
    partitioner.
  """
  return tf2xla.spmd_shard_to_full_shape(
      tensor, manual_sharding=manual_sharding, full_shape=full_shape)


def mesh_split_sharding(device_mesh, tensor_split_dims_mapping):
  """Returns a Sharding object representing sharding along multiple dimensions.

  Args:
    device_mesh: An np.ndarray describing the topology of the device mesh and
      each element is the ID of the device in the topology.
    tensor_split_dims_mapping: A list of integers that map each tensor axis to
      the device mesh axis along which it is sharded. Its length is the tensor
      rank, and tensor_split_dims_mapping[i] is device mesh axis for tensor
      dimension i. Use -1 for tensor dimensions that are not sharded.

  Raises:
    ValueError: The number of tensor split dimensions is larger than device mesh
      rank.
  """
  permutation = [d for d in tensor_split_dims_mapping if d >= 0]
  if len(permutation) > len(device_mesh.shape):
    raise ValueError(
        'Number of tensor split dimensions (%r) is larger than device mesh '
        'rank (%r). tensor_split_dims_mapping: %r, device_mesh.shape: %r' %
        (len(permutation), len(
            device_mesh.shape), tensor_split_dims_mapping, device_mesh.shape))
  # Append replicated dimensions to the end.
  transpose_permutation = permutation + [
      d for d in range(len(device_mesh.shape)) if d not in permutation
  ]
  tile_assignment = _np.transpose(device_mesh, transpose_permutation)
  tile_shape = [
      1 if d < 0 else device_mesh.shape[d] for d in tensor_split_dims_mapping
  ]
  partial = len(permutation) < len(device_mesh.shape)
  if partial:
    tile_shape.append(_np.prod(device_mesh.shape) // _np.prod(tile_shape))
  tile_assignment = _np.reshape(tile_assignment, tile_shape)

  if partial:
    return Sharding.partial_tile(tile_assignment)
  return Sharding.tile(tile_assignment)


def mesh_split(tensor,
               device_mesh,
               tensor_split_dims_mapping,
               use_sharding_op=False):
  """Returns a tensor that is split along multiple dimensions in a device mesh.

  Args:
    tensor: A tf.Tensor to split.
    device_mesh: An np.ndarray describing the topology of the device mesh and
      each element is the ID of the device in the topology.
    tensor_split_dims_mapping: A list of integers that map each tensor axis to
      the device mesh axis along which it is sharded. Its length is the tensor
      rank, and tensor_split_dims_mapping[i] is device mesh axis for tensor
      dimension i. Use -1 for tensor dimensions that are not sharded.
    use_sharding_op: If true, adds a sharding op to set the sharding.

  Raises:
    ValueError: The number of tensor split dimensions is larger than device mesh
      rank.
  """
  sharding = mesh_split_sharding(device_mesh, tensor_split_dims_mapping)
  return sharding.apply_to_tensor(tensor, use_sharding_op=use_sharding_op)
