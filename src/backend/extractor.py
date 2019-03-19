import gc
from collections import namedtuple

import numpy as np
import re
from PIL import Image

FeatureTuple = namedtuple('FeatureTuple', ['name', 'timestamp', 'features'])


class Extractor():
    def __init__(self, images, batch_size):
        self.images = _batch_images(images, batch_size)

    def __len__(self):
        return len(self.images)

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return self.extract_features(next(self.images))
        except StopIteration:
            raise StopIteration

    def extract_features(self, images):
        raise NotImplementedError(
            'Feature extractor must implement \'extract_features(self, images\' !'
        )


class TensorFlowExtractor(Extractor):
    try:
        import tensorflow as tf
        from src import tf_models
    except ImportError:
        pass

    def __init__(self, images, model_path, layer, batch_size=256, gpu=True):
        super().__init__(images, batch_size)
        self.batch_size = batch_size
        self.graph = self.__load_graph(model_path)
        self.layer = layer
        with self.graph.as_default():
            self.input, self.layers = self.__input_and_layers()
            assert self.layer in self.layers, '\'{}\' is not a valid layer. Available layers are: {}'.format(
                self.layer, sorted(list(self.layers.keys())))
            net_output = self.layers[self.layer]
            dim = self.tf.reduce_prod(self.tf.shape(net_output)[1:])
            self.features = self.tf.reshape(net_output, [-1, dim])
        config = self.tf.ConfigProto()
        config.gpu_options.allow_growth = True
        self.session = self.tf.Session(graph=self.graph, config=config)
        self.layers = self.layers.keys()

    def __load_graph(self, frozen_graph_filename):
        with self.tf.gfile.GFile(frozen_graph_filename, "rb") as f:
            graph_def = self.tf.GraphDef()
            graph_def.ParseFromString(f.read())

        with self.tf.Graph().as_default() as graph:
            self.tf.import_graph_def(graph_def, name="extractor")
        return graph

    def __input_and_layers(self):
        tensor_names = [op.name + ':0' for op in self.graph.get_operations() if not self.__ignore_tensor(op.name)]
        # tensor_endings = set([tensor.split('/')[-1] for tensor in tensor_names])

        prefix_re = r'^(\w+)/*'
        prefix = re.match(prefix_re, tensor_names[0]).group(1)

        # layer_re = r'\b(\w+)/\1\b'
        # layer_names = set(
        #     re.search(layer_re, tensor).group(1) for tensor in tensor_names
        #     if re.search(layer_re, tensor))

        layer_dict = {tensor_name.split('/')[-1][:-2]: self.graph.get_tensor_by_name(tensor_name) for tensor_name in
                      tensor_names}
        # layer_dict = {
        #     layer: self.graph.get_tensor_by_name(
        #         '/'.join([prefix] + [layer] * 2) + ':0')
        #     for layer in layer_names
        # }
        input = self.graph.get_tensor_by_name(prefix + '/input:0')
        return input, layer_dict

    def __ignore_tensor(self, tensor_name):
        ignored_tensor_endings = ['weights', 'bias', 'MatMul', 'BiasAdd', 'read', 'size', 'prob', 'stack',
                                  'strided_slice', 'shape', 'axis', 'input', 'concat', 'Conv2D', 'split', 'norm']
        return any(ending in tensor_name for ending in ignored_tensor_endings)

    def __process_images(self, images, data_spec):

        if data_spec.expects_bgr:
            # convert from RGB to BGR
            images = images[:, :, :, ::-1]

        # Rescale
        images = self.tf.image.resize_images(
            images, (data_spec.crop_size, data_spec.crop_size))
        return self.tf.to_float(images)

    def extract_features(self, tuple_batch):
        name_batch, ts_batch, image_batch = tuple_batch
        feature_batch = self.session.run(
            self.features, feed_dict={
                self.input: image_batch
            })

        return map(FeatureTuple._make, zip(name_batch, ts_batch,
                                           feature_batch))


class TensorflowHubExtractor(Extractor):
    try:
        import tensorflow as tf
        import tensorflow_hub as hub
    except ImportError:
        pass

    def __init__(self, images, model_path, layer, batch_size=256, gpu=True):
        super().__init__(images, batch_size)
        self.batch_size = batch_size
        self.layer = layer
        with self.tf.Graph().as_default() as g:
            module = self.hub.Module(model_path)
            self.__image_height, self.__image_width = self.hub.get_expected_image_size(
                module)
            self.input = self.tf.placeholder(
                self.tf.float32, shape=(None, None, None, 3))
            processed_features = self.__process_images(self.input)
            self.__layers = module(dict(images=processed_features), signature='image_feature_vector', as_dict=True)
            self.layers = self.__layers.keys()
            if self.layer in self.__layers:
                self.features = self.__layers[layer]
                config = self.tf.ConfigProto()
                config.gpu_options.allow_growth = True
                self.session = self.tf.Session(graph=g, config=config)
                self.session.run(self.tf.global_variables_initializer())
                self.session.run(self.tf.tables_initializer())

    def __process_images(self, images):

        # Rescale
        images = self.tf.image.resize_images(
            images[:, :, :, ::-1], (self.__image_height, self.__image_width))
        return images

    def extract_features(self, tuple_batch):
        name_batch, ts_batch, image_batch = tuple_batch
        feature_batch = self.session.run(
            self.features, feed_dict={
                self.input: image_batch
            })
        return map(FeatureTuple._make, zip(name_batch, ts_batch,
                                           feature_batch))


class KerasExtractor(Extractor):
    try:
        import tensorflow as tf
    except ImportError:
        pass

    @staticmethod
    def __resize(x, target_size=(224, 224)):
        if (x.shape[1], x.shape[2]) != target_size:
            x = np.array([np.array(Image.fromarray(image, mode='RGB').resize(target_size)) for image in x])
        return x

    @staticmethod
    def __preprocess_vgg(x):
        x = x[:, :, :, ::-1]
        return x

    def __init__(self, images, model_key, layer, weights_path='imagenet', batch_size=256, gpu=True):
        super().__init__(images, batch_size)
        self.models = {'vgg16': self.tf.keras.applications.vgg16.VGG16, 'vgg19': self.tf.keras.applications.vgg19.VGG19,
                       'resnet50': self.tf.keras.applications.resnet50.ResNet50,
                       'xception': self.tf.keras.applications.xception.Xception,
                       'inception_v3': self.tf.keras.applications.inception_v3,
                       'densenet121': self.tf.keras.applications.densenet.DenseNet121,
                       'densenet169': self.tf.keras.applications.densenet.DenseNet169,
                       'densenet201': self.tf.keras.applications.densenet.DenseNet201,
                       'mobilenet': self.tf.keras.applications.mobilenet.MobileNet,
                       'mobilenet_v2': self.tf.keras.applications.mobilenet_v2.MobileNetV2,
                       'nasnet_large': self.tf.keras.applications.nasnet.NASNetLarge,
                       'nasnet_mobile': self.tf.keras.applications.nasnet.NASNetMobile,
                       'inception_resnet_v2': self.tf.keras.applications.inception_resnet_v2.InceptionResNetV2}
        self.preprocessors = {'vgg16': self.__preprocess_vgg, 'vgg19': self.__preprocess_vgg,
                              'resnet50': self.tf.keras.applications.resnet50.preprocess_input,
                              'xception': self.tf.keras.applications.xception.preprocess_input,
                              'inception_v3': self.tf.keras.applications.inception_v3,
                              'densenet121': self.tf.keras.applications.densenet.preprocess_input,
                              'densenet169': self.tf.keras.applications.densenet.preprocess_input,
                              'densenet201': self.tf.keras.applications.densenet.preprocess_input,
                              'mobilenet': self.tf.keras.applications.mobilenet.preprocess_input,
                              'mobilenet_v2': self.tf.keras.applications.mobilenet_v2.preprocess_input,
                              'nasnet_large': self.tf.keras.applications.nasnet.preprocess_input,
                              'nasnet_mobile': self.tf.keras.applications.nasnet.preprocess_input,
                              'inception_resnet_v2': self.tf.keras.applications.inception_resnet_v2.preprocess_input}
        self.batch_size = batch_size
        self.layer = layer
        base_model = self.models[model_key](weights=weights_path)
        base_model.summary()
        self.layers = [layer.name for layer in base_model.layers]
        assert layer in self.layers, f'Invalid layer key. Available layers: {self.layers}'
        inputs = base_model.input
        outputs = base_model.get_layer(layer) if not hasattr(base_model.get_layer(layer),
                                                             'output') else base_model.get_layer(layer).output
        self.model = self.tf.keras.models.Model(inputs=inputs, outputs=outputs)
        self.preprocess = self.preprocessors[model_key]

    def extract_features(self, tuple_batch):
        name_batch, ts_batch, image_batch = tuple_batch
        image_batch = self.__resize(image_batch, target_size=self.model.input.shape[1:-1])
        image_batch = self.preprocess(image_batch)
        feature_batch = self.model.predict(image_batch)
        dim = np.prod(feature_batch.shape[1:])
        feature_batch = np.reshape(feature_batch, [-1, dim])

        return map(FeatureTuple._make, zip(name_batch, ts_batch,
                                           feature_batch))


class CaffeExtractor(Extractor):
    try:
        import caffe
    except ImportError:
        pass

    def __init__(self,
                 images,
                 def_path,
                 weights_path,
                 layer,
                 batch_size=256,
                 gpu=True):
        super().__init__(images, batch_size)
        self.layer = layer
        # set mode to GPU or CPU computation
        if gpu:
            self.caffe.set_device(0)
            self.caffe.set_mode_gpu()
        else:
            self.caffe.set_mode_cpu()

        self.net = self.caffe.Net(def_path, weights_path, self.caffe.TEST)
        self.transformer = self.caffe.io.Transformer({
            'data':
                self.net.blobs['data'].data.shape
        })
        self.transformer.set_transpose('data', (2, 0, 1))
        self.transformer.set_channel_swap(
            'data', (2, 1, 0))  # swap channels from RGB to BGR
        self.layers = list(self.net.blobs.keys())

    def extract_features(self, tuple_batch):
        name_batch, ts_batch, image_batch = tuple_batch
        shape = self.net.blobs['data'].shape
        self.net.blobs['data'].reshape(image_batch.shape[0], shape[1],
                                       shape[2], shape[3])
        self.net.reshape()
        image_batch = list(
            map(lambda x: self.transformer.preprocess('data', x), image_batch))
        self.net.blobs['data'].data[...] = image_batch
        self.net.forward()

        # extract features from the specified layer
        feature_batch = self.net.blobs[self.layer].data
        return list(zip(name_batch, ts_batch, feature_batch))


def _batch_images(images, batch_size=256):
    current_name_batch = []
    current_ts_batch = []
    current_image_batch = []
    index = 0
    for plot_tuple in images:

        name, ts, image = plot_tuple
        current_name_batch.append(name)
        current_ts_batch.append(ts)
        current_image_batch.append(image)
        del image
        if (index + 1) % batch_size == 0:
            name_batch, ts_batch, image_batch = current_name_batch, current_ts_batch, np.array(
                current_image_batch, dtype=np.uint8)
            current_name_batch = []
            current_ts_batch = []
            current_image_batch = []
            gc.collect()
            yield (name_batch, ts_batch, image_batch)
        index += 1

    if current_name_batch:
        name_batch, ts_batch, image_batch = current_name_batch, current_ts_batch, np.array(
            current_image_batch, dtype=np.uint8)
        gc.collect()
        yield (name_batch, ts_batch, image_batch)
    else:
        gc.collect()
        return
