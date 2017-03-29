import argparse
import configparser
import csv
from os import listdir
from os.path import join, isfile, basename, expanduser, dirname, isdir

import caffe


class Configuration:
    def __init__(self):
        # set default values
        self.model_directory = join(expanduser('~'), 'caffe-master/models/bvlc_alexnet')
        self.gpu_mode = True
        self.device_id = 0
        self.folders = []
        self.output = ''
        self.cmap = 'viridis'
        self.label_file = None
        self.labels = None
        self.label_dict = {}
        self.layer = 'fc7'
        self.chunksize = None
        self.step = None
        self.nfft = 256
        self.reduced = None
        self.size = 387
        self.files = []
        self.output_spectrograms = None
        self.net = None
        self.transformer = None

    def parse_arguments(self):
        parser = argparse.ArgumentParser(description='Extract deep spectrum features from wav files',
                                              formatter_class=argparse.ArgumentDefaultsHelpFormatter)
        required_named = parser.add_argument_group('Required named arguments')
        required_named.add_argument('-f', nargs='+', help='folder(s) where your wavs reside', required=True)
        required_named.add_argument('-o',
                                        help='the file which the features are written to. Supports csv and arff formats',
                                        required=True)
        parser.add_argument('-lf',
                                 help='csv file with the labels for the wavs in the form: \'test_001.wav, label\'. If nothing is specified here, the name(s) of the directory/directories are used as labels.',
                                 default=None)
        parser.add_argument('-cmap', default='viridis',
                                 help='define the matplotlib colour map to use for the spectrograms')
        parser.add_argument('-config',
                                 help='path to configuration file which specifies caffe model and weight files. If this file does not exist a new one is created and filled with the standard settings-',
                                 default="deep.conf")
        parser.add_argument('-layer', default='fc7',
                                 help='name of CNN layer (as defined in caffe prototxt) from which to extract the features. Supports layers with 1-D output.')
        parser.add_argument('-chunksize', default=None, type=int,
                                 help='define a chunksize in ms. wav data is split into chunks of this length before feature extraction.')
        parser.add_argument('-step', default=None, type=int,
                                help='stepsize for creating the wav segments in ms')
        parser.add_argument('-nfft', default=256,
                                 help='specify the size for the FFT window in number of samples', type=int)
        parser.add_argument('-reduced', nargs='?',
                                 help='a reduced version of the feature set is written to the given location.',
                                 default=None, const='deep_spectrum_reduced.arff')
        parser.add_argument('-labels', nargs='+',
                                 help='define labels for folders explicitly in format: labelForFirstFolder labelForSecondFolder ...',
                                 default=None)
        parser.add_argument('-specout',
                                 help='define an existing folder where spectrogram plots should be saved during feature extraction. By default, spectrograms are not saved on disk to speed up extraction.',
                                 default=None)

        args = vars(parser.parse_args())
        self.folders = args['f']
        self.cmap = args['cmap']
        self.output = args['o']
        self.label_file = args['lf']
        self.labels = args['labels']
        self.layer = args['layer']
        self.chunksize = args['chunksize']
        self.step = args['step'] if args['step'] else self.chunksize
        self.nfft = args['nfft']
        self.reduced = args['reduced']
        self.output_spectrograms = args['specout']
        self.files = [join(folder, wav_file) for folder in self.folders for wav_file in listdir(folder) if
                      isfile(join(folder, wav_file)) and (wav_file.endswith('.wav') or wav_file.endswith('.WAV'))]
        if not self.files:
            parser.error('No .wavs were found. Check the specified input paths.')
        if self.output_spectrograms and not isdir(self.output_spectrograms):
            parser.error('Spectrogram directory \'' + self.output_spectrograms + '\' does not exist.')
        if self.labels is not None and len(self.folders) != len(self.labels):
            parser.error(
                'Labels have to be specified for each folder: ' + str(len(self.folders)) + ' expected, ' + str(
                    len(self.labels)) + ' received.')
        print('Parsing labels...')
        if self.label_file is None:
            self._create_labels_from_folder_structure()
        else:
            self._read_label_file(parser)

        self._load_config(args['config'])
        self._configure_caffe(parser)

    def _read_label_file(self, parser):
        if self.label_file.endswith('.tsv'):
            reader = csv.reader(open(self.label_file), delimiter="\t")
        else:
            reader = csv.reader(open(self.label_file))
        self.label_dict = {}
        self.labels = set([])
        for row in reader:
            key = row[0]
            self.label_dict[key] = row[1]
            self.labels.add(row[1])
        file_names = set(map(basename, self.files))
        missing_labels = file_names.difference(self.label_dict)
        if missing_labels:
            parser.error('No labels for: ' + ', '.join(missing_labels))

    def _create_labels_from_folder_structure(self):
        if self.labels is None:
            wavs = [join(folder, wav_file) for folder in self.folders for wav_file in listdir(folder) if
                    isfile(join(folder, wav_file)) and (wav_file.endswith('.wav') or wav_file.endswith('.WAV'))]
            self.label_dict = {basename(wav): basename(dirname(wav)) for wav in wavs}
        else:
            self.label_dict = {wav: self.labels[folder_index] for folder_index, folder in enumerate(self.folders) for
                               wav in
                               listdir(folder) if
                               isfile(join(folder, wav)) and (wav.endswith('.wav') or wav.endswith('.WAV'))}

    def _load_config(self, conf_file):
        conf_parser = configparser.ConfigParser()
        if isfile(conf_file):
            print('Found config file '+conf_file)
            conf_parser.read(conf_file)
            conf = conf_parser['main']
            self.model_directory = conf['caffe_model_directory']
            self.gpu_mode = int(conf['gpu']) == 1
            self.device_id = int(conf['device_id'])
            self.size = int(conf['size'])
        else:
            print('Writing standard config to '+conf_file)
            conf = {'caffe_model_directory': self.model_directory,
                         'gpu': '1' if self.gpu_mode else '0',
                         'device_id': str(self.device_id),
                         'size': str(self.size)}
            conf_parser['main'] = conf
            with open(conf_file, 'w') as configfile:
                conf_parser.write(configfile)

    def _configure_caffe(self, parser):
        directory = self.model_directory
        try:
            model_defs = [join(directory, file) for file in listdir(directory) if file.endswith('deploy.prototxt')]
            if model_defs:
                model_def = model_defs[0]
                print('CaffeNet definition: ' + model_def)
            else:
                model_def = ''
                parser.error("No model definition found in " + directory + '.')
            model_weights = [join(directory, file) for file in listdir(directory)
                             if file.endswith('.caffemodel')]
            if model_weights:
                model_weights = model_weights[0]
                print('CaffeNet weights: ' + model_weights)
            else:
                parser.error("No model weights found in " + directory + '.')
            if self.gpu_mode:
                caffe.set_device(int(self.device_id))
                caffe.set_mode_gpu()
                print('Using GPU device ' + str(self.device_id))
            else:
                print('Using CPU-Mode')
                caffe.set_mode_cpu()

            print('Loading Net')
            self.net = caffe.Net(model_def, caffe.TEST, weights=model_weights)
            self.transformer = caffe.io.Transformer({'data': self.net.blobs['data'].data.shape})
            self.transformer.set_transpose('data', (2, 0, 1))
            self.transformer.set_raw_scale('data', 255)  # rescale from [0, 1] to [0, 255]
            self.transformer.set_channel_swap('data', (2, 1, 0))  # swap channels from RGB to BGR
            shape = self.net.blobs['data'].shape
            self.net.blobs['data'].reshape(1, shape[1], shape[2], shape[3])
            self.net.reshape()
        except FileNotFoundError:
            raise
