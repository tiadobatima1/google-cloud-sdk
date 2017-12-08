# Copyright 2017 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Utilities for running predictions.

Includes (from the Cloud ML SDK):
- _predict_lib

Important changes:
- Remove interfaces for TensorFlowModel (they don't change behavior).
- Set from_client(skip_preprocessing=True) and remove the pre-processing code.
"""
import base64
import collections
from contextlib import contextmanager
import inspect
import json
import logging
import os
import pydoc  # used for importing python classes from their FQN
import timeit

from _interfaces import Model
from enum import Enum
import numpy as np

import tensorflow.contrib   # pylint: disable=unused-import
from tensorflow.python.client import session as tf_session
from tensorflow.python.framework import dtypes
from tensorflow.python.saved_model import loader
from tensorflow.python.saved_model import signature_constants
from tensorflow.python.saved_model import tag_constants


# --------------------------
# prediction.prediction_lib
# --------------------------
class UserClassType(Enum):
  model_class = "model_class"
  processor_class = "processor_class"


INPUTS_KEY = "inputs"
OUTPUTS_KEY = "outputs"
# Keys for the name of the methods that the user provided `Processor`
# class should implement.
PREPROCESS_KEY = "preprocess"
POSTPROCESS_KEY = "postprocess"
FROM_MODEL_KEY = "from_model_path"

ENGINE = "Prediction-Engine"
FRAMEWORK = "Framework"
PREPROCESS_TIME = "Prediction-Preprocess-Time"
POSTPROCESS_TIME = "Prediction-Postprocess-Time"
COLUMNARIZE_TIME = "Prediction-Columnarize-Time"
UNALIAS_TIME = "Prediction-Unalias-Time"
ENCODE_TIME = "Prediction-Encode-Time"
ENGINE_RUN_TIME = "Prediction-Engine-Run-Time"
SESSION_RUN_TIME = "Prediction-Session-Run-Time"
ALIAS_TIME = "Prediction-Alias-Time"
ROWIFY_TIME = "Prediction-Rowify-Time"
# TODO(b/67586901): Consider removing INPUT_PROCESSING_TIME during cleanup.
# Only used in skl_xgb/prediction_server_lib.py.
INPUT_PROCESSING_TIME = "Prediction-Input-Processing-Time"

SESSION_RUN_ENGINE_NAME = "TF_SESSION_RUN"

PredictionErrorType = collections.namedtuple(
    "PredictionErrorType", ("message", "code"))


class PredictionError(Exception):
  """Customer exception for known prediction exception."""

  # The error code for prediction.
  FAILED_TO_LOAD_MODEL = PredictionErrorType(
      message="Failed to load model", code=0)
  INVALID_INPUTS = PredictionErrorType("Invalid inputs", code=1)
  FAILED_TO_RUN_MODEL = PredictionErrorType(
      message="Failed to run the provided model", code=2)
  INVALID_OUTPUTS = PredictionErrorType(
      message="There was a problem processing the outputs", code=3)
  INVALID_USER_CODE = PredictionErrorType(
      message="There was a problem processing the user code", code=4)
  # When adding new exception, please update the ERROR_MESSAGE_ list as well as
  # unittest.

  def __init__(self, error_code, error_detail, *args):
    super(PredictionError, self).__init__(error_code, error_detail, *args)

  @property
  def error_code(self):
    return self.args[0].code

  @property
  def error_message(self):
    return self.args[0].message

  @property
  def error_detail(self):
    return self.args[1]

  def __str__(self):
    return ("%s: %s (Error code: %d)" % (self.error_message,
                                         self.error_detail, self.error_code))


MICRO = 1000000
MILLI = 1000


class Timer(object):
  """Context manager for timing code blocks.

  The object is intended to be used solely as a context manager and not
  as a general purpose object.

  The timer starts when __enter__ is invoked on the context manager
  and stopped when __exit__ is invoked. After __exit__ is called,
  the duration properties report the amount of time between
  __enter__ and __exit__ and thus do not change. However, if any of the
  duration properties are called between the call to __enter__ and __exit__,
  then they will return the "live" value of the timer.

  If the same Timer object is re-used in multiple with statements, the values
  reported will reflect the latest call. Do not use the same Timer object in
  nested with blocks with the same Timer context manager.

  Example usage:

    with Timer() as timer:
      foo()
    print(timer.duration_secs)
  """

  def __init__(self, timer_fn=None):
    self.start = None
    self.end = None
    self._get_time = timer_fn or timeit.default_timer

  def __enter__(self):
    self.end = None
    self.start = self._get_time()
    return self

  def __exit__(self, exc_type, value, traceback):
    self.end = self._get_time()
    return False

  @property
  def seconds(self):
    now = self._get_time()
    return (self.end or now) - (self.start or now)

  @property
  def microseconds(self):
    return int(MICRO * self.seconds)

  @property
  def milliseconds(self):
    return int(MILLI * self.seconds)


class Stats(dict):
  """An object for tracking stats.

  This class is dict-like, so stats are accessed/stored like so:

    stats = Stats()
    stats["count"] = 1
    stats["foo"] = "bar"

  This class also facilitates collecting timing information via the
  context manager obtained using the "time" method. Reported timings
  are in microseconds.

  Example usage:

    with stats.time("foo_time"):
      foo()
    print(stats["foo_time"])
  """

  @contextmanager
  def time(self, name, timer_fn=None):
    with Timer(timer_fn) as timer:
      yield timer
    self[name] = timer.microseconds


def columnarize(instances):
  """Columnarize inputs.

  Each line in the input is a dictionary of input names to the value
  for that input (a single instance). For each input "column", this method
  appends each of the input values to a list. The result is a dict mapping
  input names to a batch of input data. This can be directly used as the
  feed dict during prediction.

  For example,

    instances = [{"a": [1.0, 2.0], "b": "a"},
                 {"a": [3.0, 4.0], "b": "c"},
                 {"a": [5.0, 6.0], "b": "e"},]
    batch = prediction_server_lib.columnarize(instances)
    assert batch == {"a": [[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]],
                     "b": ["a", "c", "e"]}

  Arguments:
    instances: (list of dict) where the dictionaries map input names
      to the values for those inputs.

  Returns:
    A dictionary mapping input names to values, as described above.
  """
  columns = collections.defaultdict(list)
  for instance in instances:
    for k, v in instance.iteritems():
      columns[k].append(v)
  return columns


def rowify(columns):
  """Converts columnar input to row data.

  Consider the following code:

    columns = {"prediction": np.array([1,             # 1st instance
                                       0,             # 2nd
                                       1]),           # 3rd
               "scores": np.array([[0.1, 0.9],        # 1st instance
                                   [0.7, 0.3],        # 2nd
                                   [0.4, 0.6]])}      # 3rd

  Then rowify will return the equivalent of:

    [{"prediction": 1, "scores": [0.1, 0.9]},
     {"prediction": 0, "scores": [0.7, 0.3]},
     {"prediction": 1, "scores": [0.4, 0.6]}]

  (each row is yielded; no list is actually created).

  Arguments:
    columns: (dict) mapping names to numpy arrays, where the arrays
      contain a batch of data.

  Raises:
    PredictionError: if the outer dimension of each input isn't identical
    for each of element.

  Yields:
    A map with a single instance, as described above. Note: instances
    is not a numpy array.
  """
  sizes_set = {e.shape[0] for e in columns.itervalues()}

  # All the elements in the length array should be identical. Otherwise,
  # raise an exception.
  if len(sizes_set) != 1:
    sizes_dict = {name: e.shape[0] for name, e in columns.iteritems()}
    raise PredictionError(
        PredictionError.INVALID_OUTPUTS,
        "Bad output from running tensorflow session: outputs had differing "
        "sizes in the batch (outer) dimension. See the outputs and their "
        "size: %s. Check your model for bugs that effect the size of the "
        "outputs." % sizes_dict)
  # Pick an arbitrary value in the map to get it's size.
  num_instances = len(next(columns.itervalues()))
  for row in xrange(num_instances):
    yield {name: output[row, ...].tolist()
           for name, output in columns.iteritems()}


def canonicalize_single_tensor_input(instances, tensor_name):
  """Canonicalize single input tensor instances into list of dicts.

  Instances that are single input tensors may or may not be provided with their
  tensor name. The following are both valid instances:
    1) instances = [{"x": "a"}, {"x": "b"}, {"x": "c"}]
    2) instances = ["a", "b", "c"]
  This function canonicalizes the input instances to be of type 1).

  Arguments:
    instances: single input tensor instances as supplied by the user to the
      predict method.
    tensor_name: the expected name of the single input tensor.

  Raises:
    PredictionError: if the wrong tensor name is supplied to instances.

  Returns:
    A list of dicts. Where each dict is a single instance, mapping the
    tensor_name to the value (as supplied by the original instances).
  """

  # Input is a single string tensor, the tensor name might or might not
  # be given.
  # There are 3 cases (assuming the tensor name is "t", tensor = "abc"):
  # 1) {"t": "abc"}
  # 2) "abc"
  # 3) {"y": ...} --> wrong tensor name is given.
  def parse_single_tensor(x, tensor_name):
    if not isinstance(x, dict):
      # case (2)
      return {tensor_name: x}
    elif len(x) == 1 and tensor_name == x.keys()[0]:
      # case (1)
      return x
    else:
      raise PredictionError(PredictionError.INVALID_INPUTS,
                            "Expected tensor name: %s, got tensor name: %s." %
                            (tensor_name, x.keys()))

  if not isinstance(instances, list):
    instances = [instances]
  instances = [parse_single_tensor(x, tensor_name) for x in instances]
  return instances


class BaseModel(object):
  """The base definition of a Model interface.
  """

  def __init__(self, client):
    """Constructs a BaseModel.

    Args:
      client: An instance of PredictionClient for performing prediction.
    """
    self._client = client
    self._user_processor = None

  def preprocess(self, instances, stats=None):
    """Runs the preprocessing function on the instances.

    Args:
      instances: list of instances as provided to the predict() method.
      stats: Stats object for recording timing information.

    Returns:
      A new list of preprocessed instances. Each instance is as described
      in the predict() method.
    """
    pass

  def postprocess(self, predicted_output, original_input=None, stats=None):
    """Runs the postprocessing function on the instances.

    Args:
      predicted_output: list of instances returned by the predict() method on
        preprocessed instances.
       original_input: List of instances, before any pre-processing was applied.
      stats: Stats object for recording timing information.

    Returns:
      A new list of postprocessed instances.
    """
    pass

  def predict(self, instances, stats=None):
    """Runs preprocessing, predict, and postprocessing on the input."""

    stats = stats or Stats()

    with stats.time(PREPROCESS_TIME):
      preprocessed = self.preprocess(instances, stats)
    with stats.time(ENGINE_RUN_TIME):
      predicted_outputs = self._client.predict(preprocessed, stats)
    with stats.time(POSTPROCESS_TIME):
      postprocessed = self.postprocess(
          predicted_outputs, original_input=instances, stats=stats)
    return instances, postprocessed

  def signature(self):
    pass


# TODO(b/34686738): when we no longer load the model to get the signature
# consider making this a named constructor on SessionClient.
def load_model(
    model_path,
    tags=(tag_constants.SERVING,),
    signature_name=signature_constants.DEFAULT_SERVING_SIGNATURE_DEF_KEY,
    config=None):
  """Loads the model at the specified path.

  Args:
    model_path: the path to either session_bundle or SavedModel
    tags: the tags that determines the model to load.
    signature_name: the string used as the key to signature map to locate the
                   serving signature.
    config: tf.ConfigProto containing session configuration options.

  Returns:
    A pair of (Session, SignatureDef) objects.

  Raises:
    PredictionError: if the model could not be loaded.
  """
  if loader.maybe_saved_model_directory(model_path):
    try:
      session = tf_session.Session(target="", graph=None, config=config)
      meta_graph = loader.load(session, tags=list(tags), export_dir=model_path)
    except Exception:  # pylint: disable=broad-except
      raise PredictionError(PredictionError.FAILED_TO_LOAD_MODEL,
                            "Failed to load the model due to bad model data."
                            " tags: %s" % tags)
  else:
    raise PredictionError(PredictionError.FAILED_TO_LOAD_MODEL,
                          "Cloud ML only supports TF 1.0 or above and models "
                          "saved in SavedModel format.")

  if session is None:
    raise PredictionError(PredictionError.FAILED_TO_LOAD_MODEL,
                          "Failed to create session when loading the model")
  signature = _get_signature_from_meta_graph(
      session.graph, meta_graph, signature_name)

  return session, signature


def _get_signature_from_meta_graph(
    graph, meta_graph,
    signature_name=signature_constants.DEFAULT_SERVING_SIGNATURE_DEF_KEY):
  """Returns the SignatureDef in meta_graph update dtypes using graph."""
  if not meta_graph.signature_def:
    raise PredictionError(PredictionError.FAILED_TO_LOAD_MODEL,
                          "MetaGraph must have at least one signature_def.")

  named_key = "serving_default_from_named"
  if len(meta_graph.signature_def) > 1:
    logging.warning("MetaGraph has multiple signatures %d. Support for "
                    "multiple signatures is limited. By default we select "
                    "named signatures.", len(meta_graph.signature_def))
  if named_key in meta_graph.signature_def:
    return meta_graph.signature_def[named_key]

  if signature_name not in meta_graph.signature_def:
    raise PredictionError(
        PredictionError.FAILED_TO_LOAD_MODEL,
        "No signature found for signature key %s." % signature_name)

  signature = meta_graph.signature_def[signature_name]
  # Signatures often omit the dtype and shape information. Looks those up if
  # necessary.
  _update_dtypes(graph, signature.inputs)
  _update_dtypes(graph, signature.outputs)

  return signature


def _update_dtypes(graph, interface):
  """Adds dtype to TensorInfos in interface if necessary.

  If already present, validates TensorInfo matches values in the graph.
  TensorInfo is updated in place.

  Args:
    graph: the TensorFlow graph; used to lookup datatypes of tensors.
    interface: map from alias to TensorInfo object.

  Raises:
    ValueError: if the data type in the TensorInfo does not match the type
      found in graph.
  """
  for alias, info in interface.iteritems():
    # Postpone conversion to enum for better error messages.
    dtype = graph.get_tensor_by_name(info.name).dtype
    if not info.dtype:
      info.dtype = dtype.as_datatype_enum
    elif info.dtype != dtype.as_datatype_enum:
      raise ValueError("Specified data types do not match for alias %s. "
                       "Graph has %d while TensorInfo reports %d." %
                       (alias, dtype, info.dtype))


class SessionClient(object):
  """A client for Prediction that uses Session.run."""

  def __init__(self, session, signature):
    self._session = session
    self._signature = signature

    # TensorFlow requires a bonefide list for the fetches. To regenerating the
    # list every prediction, we cache the list of output tensor names.
    self._output_tensors = [v.name for v in self._signature.outputs.values()]

  @property
  def signature(self):
    return self._signature

  def predict(self, inputs, stats):
    """Produces predictions for the given inputs.

    Args:
      inputs: a dict mapping input names to values
      stats: Stats object for recording timing information.

    Returns:
      A dict mapping output names to output values, similar to the input
      dict.
    """
    stats[ENGINE] = "SessionRun"
    stats[FRAMEWORK] = "TENSORFLOW"

    with stats.time(UNALIAS_TIME):
      try:
        unaliased = {self.signature.inputs[key].name: val
                     for key, val in inputs.iteritems()}
      except Exception as e:
        raise PredictionError(PredictionError.INVALID_INPUTS,
                              "Input mismatch: " + str(e))

    with stats.time(SESSION_RUN_TIME):
      try:
        # TODO(b/33849399): measure the actual session.run() time, even in the
        # case of ModelServer.
        outputs = self._session.run(fetches=self._output_tensors,
                                    feed_dict=unaliased)
      except Exception as e:
        logging.error("Exception during running the graph: " + str(e))
        raise PredictionError(PredictionError.FAILED_TO_RUN_MODEL,
                              "Exception during running the graph: " + str(e))

    with stats.time(ALIAS_TIME):
      return dict(zip(self._signature.outputs.iterkeys(), outputs))


def create_model(client, model_path, **kwargs):
  """Creates and returns the appropriate model.

  Creates and returns the TensorFlowModel if no user specified model is
  provided. Otherwise, the user specified model is imported, created, and
  returned.

  Args:
    client: An instance of ModelServerClient for performing prediction.
    model_path: the path to either session_bundle or SavedModel
    **kwargs: keyword arguments to pass to TensorFlowModel.from_client.

  Returns:
    An instance of the appropriate model class.
  """
  return (load_model_class(client, model_path) or
          TensorFlowModel.from_client(client, model_path, **kwargs))


def load_model_class(client, model_path):
  """Loads in the user specified custom Model class.

  Args:
    client: An instance of ModelServerClient for performing prediction.
    model_path: the path to either session_bundle or SavedModel

  Returns:
    An instance of a Model.
    Returns None if the user didn't specify the name of the custom
    python class to load in the create_version_request.

  Raises:
    PredictionError: for any of the following:
      (1) the user provided python model class cannot be found
      (2) if the loaded class does not implement the Model interface.
  """
  model_class = load_custom_class(UserClassType.model_class)
  if not model_class:
    return None
  model_instance = model_class.from_client(client, model_path)
  _validate_model_class(model_instance)
  return model_instance


def load_custom_class(class_type):
  """Loads in the user specified custom class.

  Args:
    class_type: An instance of UserClassType specifying what type of class to
    load.

  Returns:
    An instance of a class specified by the user in the `create_version_request`
    or None if no such class was specified.

  Raises:
    PredictionError: if the user provided python class cannot be found.
  """
  create_version_json = os.environ.get("create_version_request")
  if not create_version_json:
    return None
  create_version_request = json.loads(create_version_json)
  if not create_version_request:
    return None
  version = create_version_request.get("version")
  if not version:
    return None
  class_name = version.get(class_type.name)
  if not class_name:
    return None
  custom_class = pydoc.locate(class_name)
  # TODO(b/37749453): right place to generate errors?
  if not custom_class:
    package_uris = [str(s) for s in version.get("package_uris")]
    raise PredictionError(PredictionError.INVALID_USER_CODE,
                          "%s cannot be found. Please make sure "
                          "(1) %s is the fully qualified function "
                          "name, and (2) %s uses the correct package "
                          "name as provided by the package_uris: %s" %
                          (class_name, class_type.name, class_type.name,
                           package_uris))
  return custom_class


def _validate_model_class(user_class):
  """Validates a user provided instance of a Model implementation.

  Args:
    user_class: An instance of a Model implementation.

  Raises:
    PredictionError: for any of the following:
      (1) the user model class does not have the correct method signatures for
      the predict method
      (2) the user model class does not implement the signature method
  """
  user_class_name = type(user_class).__name__
  # Can't use isinstance() because the user doesn't have access to our Model
  # class. We can only inspect the user_class to check if it conforms to the
  # Model interface.
  if not hasattr(user_class, "predict"):
    raise PredictionError(PredictionError.INVALID_USER_CODE,
                          "The provided model class, %s, is missing the "
                          "required predict method." % user_class_name)
  # Check that the signature method is implemented
  if not hasattr(user_class, "signature"):
    raise PredictionError(PredictionError.INVALID_USER_CODE,
                          "The provided model class, %s, is missing the "
                          "required signature property." % user_class_name)
  # Check the predict method has the correct number of arguments
  user_signature = inspect.getargspec(user_class.predict)[0]
  model_signature = inspect.getargspec(Model.predict)[0]
  user_predict_num_args = len(user_signature)
  predict_num_args = len(model_signature)
  if predict_num_args is not user_predict_num_args:
    raise PredictionError(PredictionError.INVALID_USER_CODE,
                          "The provided model class, %s, has a predict method "
                          "with an invalid signature. Expected signature: %s "
                          "User signature: %s" %
                          (user_class_name, model_signature, user_signature))


# TODO(user): Make this generic so it can load any Processor class, not just
# from the create_version_request.
def _new_processor_class(model_path=None):
  user_processor_cls = load_custom_class(UserClassType.processor_class)
  if user_processor_cls:
    user_preprocess_fn = getattr(user_processor_cls, PREPROCESS_KEY, None)
    user_postprocess_fn = getattr(user_processor_cls, POSTPROCESS_KEY, None)
    user_from_model_path_fn = getattr(user_processor_cls, FROM_MODEL_KEY, None)

    _validate_fn_signature(user_preprocess_fn, ["self", "instances"],
                           PREPROCESS_KEY, user_processor_cls.__name__)
    _validate_fn_signature(user_postprocess_fn, ["self", "instances"],
                           POSTPROCESS_KEY, user_processor_cls.__name__)
    _validate_fn_signature(user_from_model_path_fn, ["cls", "model_path"],
                           FROM_MODEL_KEY, user_processor_cls.__name__)
    if user_from_model_path_fn:
      return user_from_model_path_fn(model_path)  # pylint: disable=not-callable
    # Call the constructor if no `from_model_path` method provided.
    return user_processor_cls()


def _validate_fn_signature(fn, required_arg_names, expected_fn_name, cls_name):
  if not fn:
    return
  if not callable(fn):
    raise PredictionError(
        PredictionError.INVALID_USER_CODE,
        "The provided %s function in the Processor class "
        "%s is not callable." % (expected_fn_name, cls_name))
  for arg in required_arg_names:
    if arg not in inspect.getargspec(fn).args:
      raise PredictionError(
          PredictionError.INVALID_USER_CODE,
          "The provided %s function in the Processor class "
          "has an invalid signature. It should take %s as arguments but"
          "takes %s" %
          (fn.__name__, required_arg_names, inspect.getargspec(fn).args))


class TensorFlowModel(BaseModel):
  """The default implementation of the Model interface that uses TensorFlow.

  This implementation optionally performs preprocessing and postprocessing
  using the provided functions. These functions accept a single instance
  as input and produce a corresponding output to send to the prediction
  client.
  """

  def __init__(self, client, preprocess_fn=None, postprocess_fn=None):
    """Constructs a TensorFlowModel.

    Args:
      client: An instance of ModelServerClient for performing prediction.
      preprocess_fn: a function to run on each instance before calling predict,
          if this parameter is not None. See class docstring.
      postprocess_fn: a function to run on each instance after calling predict,
          if this parameter is not None. See class docstring.
    """
    super(TensorFlowModel, self).__init__(client)
    self._preprocess_fn = preprocess_fn
    self._postprocess_fn = postprocess_fn

  def _get_columns(self, instances, stats):
    """Columnarize the instances, appending input_name, if necessary.

    Instances are the same instances passed to the predict() method. Since
    models with a single input can accept the raw input without the name,
    we create a dict here with that name.

    This list of instances is then converted into a column-oriented format:
    The result is a dictionary mapping input name to a list of values for just
    that input (one entry per row in the original instances list).

    Args:
      instances: the list of instances as provided to the predict() method.
      stats: Stats object for recording timing information.

    Returns:
      A dictionary mapping input names to their values.

    Raises:
      PredictionError: if an error occurs during prediction.
    """
    with stats.time(COLUMNARIZE_TIME):
      columns = columnarize(instances)
      for k, v in columns.iteritems():
        if k not in self._client.signature.inputs.keys():
          raise PredictionError(
              PredictionError.INVALID_INPUTS,
              "Unexpected tensor name: %s" % k)
        # Detect whether or not the user omits an input in one or more inputs.
        # TODO(b/34686738): perform this check in columnarize?
        if isinstance(v, list) and len(v) != len(instances):
          raise PredictionError(
              PredictionError.INVALID_INPUTS,
              "Input %s was missing in at least one input instance." % k)
    return columns

  # TODO(b/34686738): can this be removed?
  def is_single_input(self):
    """Returns True if the graph only has one input tensor."""
    return len(self._client.signature.inputs) == 1

  # TODO(b/34686738): can this be removed?
  def is_single_string_input(self):
    """Returns True if the graph only has one string input tensor."""
    if self.is_single_input():
      dtype = self._client.signature.inputs.values()[0].dtype
      return dtype == dtypes.string.as_datatype_enum
    return False

  def preprocess(self, instances, stats):
    preprocessed = self._canonicalize_input(instances)
    if self._preprocess_fn:
      try:
        preprocessed = self._preprocess_fn(preprocessed)
      except Exception as e:
        logging.error("Exception during preprocessing: " + str(e))
        raise PredictionError(PredictionError.INVALID_INPUTS,
                              "Exception during preprocessing: " + str(e))
    return self._get_columns(preprocessed, stats)

  def _canonicalize_input(self, instances):
    """Preprocess single-input instances to be dicts if they aren't already."""
    # The instances should be already (b64-) decoded here.
    if not self.is_single_input():
      return instances
    tensor_name = self._client.signature.inputs.keys()[0]
    return canonicalize_single_tensor_input(instances, tensor_name)

  def postprocess(self, predicted_output, original_input=None, stats=None):
    """Performs the necessary transformations on the prediction results.

    The transformations include rowifying the predicted results, and also
    making sure that each input/output is a dict mapping input/output alias to
    the value for that input/output.

    Args:
      predicted_output: list of instances returned by the predict() method on
        preprocessed instances.
      original_input: List of instances, before any pre-processing was applied.
      stats: Stats object for recording timing information.

    Returns:
      A list which is a dict mapping output alias to the output.
    """
    stats = stats or Stats()
    with stats.time(ROWIFY_TIME):
      # When returned element only contains one result (batch size == 1),
      # tensorflow's session.run() will return a scalar directly instead of a
      # a list. So we need to listify that scalar.
      # TODO(b/34686738): verify this behavior is correct.
      def listify(value):
        if not hasattr(value, "shape"):
          return np.asarray([value], dtype=np.object)
        elif not value.shape:
          # TODO(b/34686738): pretty sure this is a bug that only exists because
          # samples like iris have a bug where they use tf.squeeze which removes
          # the batch dimension. The samples should be fixed.
          return np.expand_dims(value, axis=0)
        else:
          return value

      postprocessed_outputs = {
          alias: listify(val)
          for alias, val in predicted_output.iteritems()
      }
      postprocessed_outputs = rowify(postprocessed_outputs)

    postprocessed_outputs = list(postprocessed_outputs)
    if self._postprocess_fn:
      try:
        postprocessed_outputs = self._postprocess_fn(postprocessed_outputs)
      except Exception as e:
        logging.error("Exception during postprocessing: %s", e)
        raise PredictionError(PredictionError.INVALID_INPUTS,
                              "Exception during postprocessing: " + str(e))

    with stats.time(ENCODE_TIME):
      try:
        postprocessed_outputs = encode_base64(postprocessed_outputs,
                                              self._client.signature.outputs)
      except PredictionError as e:
        logging.error("Encode base64 failed: %s", e)
        raise PredictionError(PredictionError.INVALID_OUTPUTS,
                              "Prediction failed during encoding instances: {0}"
                              .format(e.error_detail))
      except ValueError as e:
        logging.error("Encode base64 failed: %s", e)
        raise PredictionError(PredictionError.INVALID_OUTPUTS,
                              "Prediction failed during encoding instances: {0}"
                              .format(e))
      except Exception as e:  # pylint: disable=broad-except
        logging.error("Encode base64 failed: %s", e)
        raise PredictionError(PredictionError.INVALID_OUTPUTS,
                              "Prediction failed during encoding instances")

      return postprocessed_outputs

  # TODO(b/34686738): use signatures instead; remove this method.
  def outputs_type_map(self):
    """Returns a map from tensor alias to tensor type."""
    return {alias: dtypes.DType(info.dtype)
            for alias, info in self._client.signature.outputs.iteritems()}

  # TODO(b/34686738). Seems like this should be split into helper methods:
  #   default_preprocess_fn(model_path, skip_preprocessing) and
  #   default_model_and_preprocessor.
  @classmethod
  def from_client(cls, client, unused_model_path, **unused_kwargs):
    """Creates a TensorFlowModel from a SessionClient and model data files."""
    processor_cls = _new_processor_class()
    if processor_cls:
      return cls(client,
                 getattr(processor_cls, PREPROCESS_KEY, None),
                 getattr(processor_cls, POSTPROCESS_KEY, None))
    else:
      return cls(client)

  @property
  def signature(self):
    return self._client.signature


class SklearnModel(BaseModel):
  """The implementation of Scikit-learn Model.
  """

  def __init__(self, client):
    super(SklearnModel, self).__init__(client)
    self._user_processor = _new_processor_class()
    if self._user_processor and hasattr(self._user_processor, PREPROCESS_KEY):
      self._preprocess = self._user_processor.preprocess
    else:
      self._preprocess = self._null_processor
    if self._user_processor and hasattr(self._user_processor, POSTPROCESS_KEY):
      self._postprocess = self._user_processor.postprocess
    else:
      self._postprocess = self._null_processor

  def preprocess(self, instances, stats=None):
    # TODO(b/67383676) Consider changing this to a more generic type.
    return self._preprocess(np.array(instances))

  def postprocess(self, predicted_outputs, original_input=None, stats=None):
    # TODO(b/67383676) Consider changing this to a more generic type.
    post_processed = self._postprocess(predicted_outputs)
    if isinstance(post_processed, np.ndarray):
      return post_processed.tolist()
    if isinstance(post_processed, list):
      return post_processed
    raise PredictionError(
        PredictionError.INVALID_OUTPUTS,
        "Bad output type returned after running %s"
        "The post-processing function should return either "
        "a numpy ndarray or a list."
        % self._postprocess.__name__)

  def _null_processor(self, instances):
    return instances

  @property
  def signature(self):
    return None


class XGBoostModel(SklearnModel):
  """The implementation of XGboost Model.
  """

  def __init__(self, client):
    super(XGBoostModel, self).__init__(client)


def decode_base64(data):
  if isinstance(data, list):
    return [decode_base64(val) for val in data]
  elif isinstance(data, dict):
    if data.viewkeys() == {"b64"}:
      return base64.b64decode(data["b64"])
    else:
      return {k: decode_base64(v) for k, v in data.iteritems()}
  else:
    return data


def encode_base64(instances, outputs_map):
  """Encodes binary data in a JSON-friendly way."""
  if not isinstance(instances, list):
    raise ValueError("only lists allowed in output; got %s" %
                     (type(instances),))

  if not instances:
    return instances

  first_value = instances[0]
  if not isinstance(first_value, dict):
    if len(outputs_map) != 1:
      return ValueError("The first instance was a string, but there are "
                        "more than one output tensor, so dict expected.")
    # Only string tensors whose name ends in _bytes needs encoding.
    tensor_name, tensor_info = outputs_map.items()[0]
    tensor_type = tensor_info.dtype
    if tensor_type == dtypes.string and tensor_name.endswith("_bytes"):
      instances = _encode_str_tensor(instances)
    return instances

  encoded_data = []
  for instance in instances:
    encoded_instance = {}
    for tensor_name, tensor_info in outputs_map.iteritems():
      tensor_type = tensor_info.dtype
      tensor_data = instance[tensor_name]
      if tensor_type == dtypes.string and tensor_name.endswith("_bytes"):
        tensor_data = _encode_str_tensor(tensor_data)
      encoded_instance[tensor_name] = tensor_data
    encoded_data.append(encoded_instance)
  return encoded_data


def _encode_str_tensor(data):
  if isinstance(data, list):
    return [_encode_str_tensor(val) for val in data]
  return {"b64": base64.b64encode(data)}


def local_predict(
    model_dir=None,
    tags=(tag_constants.SERVING,),
    signature_name=signature_constants.DEFAULT_SERVING_SIGNATURE_DEF_KEY,
    instances=None):
  """Run a prediction locally."""
  instances = decode_base64(instances)
  client = SessionClient(*load_model(model_dir, tags, signature_name))
  model = create_model(client, model_dir)
  _, predictions = model.predict(instances)
  predictions = list(predictions)
  predictions = encode_base64(predictions, model.signature.outputs)
  return {"predictions": predictions}
