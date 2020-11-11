#!/usr/bin/env python3
"""
 Copyright (c) 2020 Intel Corporation
 Licensed under the Apache License, Version 2.0 (the "License");
 you may not use this file except in compliance with the License.
 You may obtain a copy of the License at
      http://www.apache.org/licenses/LICENSE-2.0
 Unless required by applicable law or agreed to in writing, software
 distributed under the License is distributed on an "AS IS" BASIS,
 WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 See the License for the specific language governing permissions and
 limitations under the License.
"""

import asyncio
import logging as log
import os
import subprocess
import sys
import tempfile
from argparse import SUPPRESS, ArgumentParser
from enum import Enum

import cv2 as cv
import numpy as np

#TODO: check sympy rendering
try:
    import sympy
    RENDER=True
except ImportError:
    RENDER=False
from openvino.inference_engine import IECore
from tqdm import tqdm
from utils import END_TOKEN, START_TOKEN, Vocab

CONFIDENCE_THRESH = 0.8
DEFAULT_RESOLUTION = (1280, 720)
DEFAULT_WIDTH = 800
MIN_HEIGHT = 30
MAX_HEIGHT = 111
MAX_WIDTH = 970
MIN_WIDTH = 260


class ModelStatus(Enum):
    ready = 0
    encoder_infer = 1
    decoder_infer = 2


def check_environment():
    command = subprocess.run(["pdflatex", "--version"], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, check=False, shell=True)
    if command.stderr:
        raise EnvironmentError("pdflatex not installed, please install it: \n{}".format(command.stderr))
    command = subprocess.run(["gs", "--version"], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, check=False, shell=True)
    if command.stderr:
        raise EnvironmentError("ghostscript not installed, please install it: \n{}".format(command.stderr))
    command = subprocess.run(["convert", "--version"], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, check=False, shell=True)
    if command.stderr:
        raise EnvironmentError("imagemagick not installed, please install it: \n{}".format(command.stderr))


def crop(img, target_shape):
    target_height, target_width = target_shape
    img_h, img_w = img.shape[0:2]
    new_w = min(target_width, img_w)
    new_h = min(target_height, img_h)
    return img[:new_h, :new_w, :]


def resize(img, target_shape):
    target_height, target_width = target_shape
    img_h, img_w = img.shape[0:2]
    scale = min(target_height / img_h, target_width / img_w)
    return cv.resize(img, None, fx=scale, fy=scale)


PREPROCESSING = {
    'crop': crop,
    'resize': resize
}

COLOR_WHITE = (255, 255, 255)


def read_net(model_xml, ie, device):
    model_bin = os.path.splitext(model_xml)[0] + ".bin"

    log.info("Loading network files:\n\t{}\n\t{}".format(model_xml, model_bin))
    model = ie.read_network(model_xml, model_bin)
    return model


def print_stats(module):
    perf_counts = module.requests[0].get_perf_counts()
    print('{:<70} {:<15} {:<15} {:<15} {:<10}'.format('name', 'layer_type', 'exet_type', 'status',
                                                      'real_time, us'))
    for layer, stats in perf_counts.items():
        print('{:<70} {:<15} {:<15} {:<15} {:<10}'.format(layer, stats['layer_type'], stats['exec_type'],
                                                          stats['status'], stats['real_time']))


def change_layout(model_input):
    model_input = model_input.transpose((2, 0, 1))
    model_input = np.expand_dims(model_input, axis=0)
    return model_input


def preprocess_image(preprocess, image_raw, tgt_shape):
    target_height, target_width = tgt_shape
    image_raw = preprocess(image_raw, tgt_shape)
    img_h, img_w = image_raw.shape[0:2]
    image_raw = cv.copyMakeBorder(image_raw, 0, target_height - img_h,
                                  0, target_width - img_w, cv.BORDER_CONSTANT,
                                  None, COLOR_WHITE)
    return image_raw


class Demo:
    def __init__(self, args):
        self.args = args
        log.info("Creating Inference Engine")
        self.ie = IECore()
        self.ie.set_config(
            {"PERF_COUNT": "YES" if self.args.perf_counts else "NO"}, args.device)
        # check_environment()
        self.encoder = read_net(self.args.m_encoder, self.ie, self.args.device)
        self.dec_step = read_net(self.args.m_decoder, self.ie, self.args.device)
        self.exec_net_encoder = self.ie.load_network(network=self.encoder, device_name=self.args.device)
        self.exec_net_decoder = self.ie.load_network(network=self.dec_step, device_name=self.args.device)
        self.images_list = []
        self.vocab = Vocab(self.args.vocab_path)
        self.model_status = ModelStatus.ready
        self.is_async = args.interactive
        if not args.interactive:
            self.preprocess_inputs()

    def preprocess_inputs(self):
        batch_dim, channels, height, width = self.encoder.input_info['imgs'].input_data.shape
        assert batch_dim == 1, "Demo only works with batch size 1."
        assert channels in (1, 3), "Input image is not 1 or 3 channeled image."
        target_shape = (height, width)
        if os.path.isdir(self.args.input):
            inputs = sorted(os.path.join(self.args.input, inp)
                            for inp in os.listdir(self.args.input))
        else:
            inputs = [self.args.input]
        log.info("Loading and preprocessing images")
        for filenm in tqdm(inputs):
            image_raw = cv.imread(filenm)
            assert image_raw is not None, "Error reading image {}".format(filenm)
            image = preprocess_image(
                PREPROCESSING[self.args.preprocessing_type], image_raw, target_shape)
            record = dict(img_name=filenm, img=image, formula=None)
            self.images_list.append(record)

    def _async_infer_encoder(self, image, req_id):
        return self.exec_net_encoder.start_async(request_id=req_id, inputs={self.args.imgs_layer: image})

    def _async_infer_decoder(self, row_enc_out, dec_st_c, dec_st_h, output, tgt, req_id):
        return self.exec_net_decoder.start_async(request_id=req_id, inputs={self.args.row_enc_out_layer: row_enc_out,
                                                                            self.args.dec_st_c_layer: dec_st_c,
                                                                            self.args.dec_st_h_layer: dec_st_h,
                                                                            self.args.output_prev_layer: output,
                                                                            self.args.tgt_layer: tgt
                                                                            }
                                                 )

    def infer_async(self, model_input):
        model_input = change_layout(model_input)
        assert self.is_async
        # asynchronous variant
        if self.model_status == ModelStatus.ready:
            infer_status_encoder = self._run_encoder(model_input)
            return None

        if self.model_status == ModelStatus.encoder_infer:
            infer_status_encoder = self._infer_request_handle_encoder.wait(timeout=1)
            if infer_status_encoder == 0:
                self._run_decoder()
            return None

        return self._process_decoding_results()

    def infer_sync(self, model_input):
        assert not self.is_async
        model_input = change_layout(model_input)
        self._run_encoder(model_input)
        self._run_decoder()
        res = None
        while res is None:
            res = self._process_decoding_results()
        return res

    def _process_decoding_results(self):
        timeout = 1 if self.is_async else -1
        infer_status_decoder = self._infer_request_handle_decoder.wait(timeout)
        if infer_status_decoder != 0 and self.is_async:
            return None
        dec_res = self._infer_request_handle_decoder.output_blobs
        self._unpack_dec_results(dec_res)

        if self.tgt[0][0][0] == END_TOKEN:
            self.logits = np.array(self.logits)
            logits = self.logits.squeeze(axis=1)
            targets = np.argmax(logits, axis=1)
            self.model_status = ModelStatus.ready
            return logits, targets
        self._infer_request_handle_decoder = self._async_infer_decoder(self.row_enc_out,
                                                                       self.dec_states_c,
                                                                       self.dec_states_h,
                                                                       self.output,
                                                                       self.tgt,
                                                                       req_id=0
                                                                       )

        return None

    def _run_encoder(self, model_input):
        timeout = 1 if self.is_async else -1
        self._infer_request_handle_encoder = self._async_infer_encoder(model_input, req_id=0)
        self.model_status = ModelStatus.encoder_infer
        infer_status_encoder = self._infer_request_handle_encoder.wait(timeout=timeout)
        return infer_status_encoder

    def _run_decoder(self):
        enc_res = self._infer_request_handle_encoder.output_blobs
        self._unpack_enc_results(enc_res)
        self._infer_request_handle_decoder = self._async_infer_decoder(
            self.row_enc_out, self.dec_states_c, self.dec_states_h, self.output, self.tgt, req_id=0)
        self.model_status = ModelStatus.decoder_infer

    def _unpack_dec_results(self, dec_res):
        self.dec_states_h = dec_res[self.args.dec_st_h_t_layer].buffer
        self.dec_states_c = dec_res[self.args.dec_st_c_t_layer].buffer
        self.output = dec_res[self.args.output_layer].buffer
        logit = dec_res[self.args.logit_layer].buffer
        self.logits.append(logit)
        self.tgt = np.array([[np.argmax(logit, axis=1)]])

    def _unpack_enc_results(self, enc_res):
        self.row_enc_out = enc_res[self.args.row_enc_out_layer].buffer
        self.dec_states_h = enc_res[self.args.hidden_layer].buffer
        self.dec_states_c = enc_res[self.args.context_layer].buffer
        self.output = enc_res[self.args.init_0_layer].buffer
        self.tgt = np.array([[START_TOKEN]])
        self.logits = []


def create_videocapture(resolution=DEFAULT_RESOLUTION):
    capture = cv.VideoCapture(0)
    capture.set(cv.CAP_PROP_BUFFERSIZE, 1)
    capture.set(3, resolution[0])
    capture.set(4, resolution[1])
    return capture


def create_input_window(input_shape, aspect_ratio):
    # height, width = input_shape
    default_width = DEFAULT_WIDTH
    height = int(default_width * aspect_ratio)
    start_point = (int(DEFAULT_RESOLUTION[0] / 2 - default_width / 2), int(DEFAULT_RESOLUTION[1] / 2 - height / 2))
    end_point = (int(DEFAULT_RESOLUTION[0] / 2 + default_width / 2), int(DEFAULT_RESOLUTION[1] / 2 + height / 2))
    return start_point, end_point


def get_crop(frame, start_point, end_point):
    crop = frame[start_point[1]:end_point[1], start_point[0]:end_point[0], :]
    return crop


def draw_rectangle(frame, start_point, end_point, color=(0, 0, 255), thickness=2):
    frame = cv.rectangle(frame, start_point, end_point, color, thickness)
    return frame


def resize_window(action, start_point, end_point, aspect_ratio):
    height = end_point[1] - start_point[1]
    width = end_point[0] - start_point[0]

    if action == 'increase':
        if height >= MAX_HEIGHT or width >= MAX_WIDTH:
            return start_point, end_point
        start_point = (start_point[0]-10, start_point[1] - int(10 * aspect_ratio))
        end_point = (end_point[0]+10, end_point[1] + int(10 * aspect_ratio))
    elif action == 'decrease':
        if height <= MIN_HEIGHT or width <= MIN_WIDTH:
            return start_point, end_point
        start_point = (start_point[0]+10, start_point[1] + int(10 * aspect_ratio))
        end_point = (end_point[0]-10, end_point[1] - int(10 * aspect_ratio))
    else:
        raise ValueError(f"wrong action: {action}")
    return start_point, end_point


def put_text(frame, start_point, text):
    (txt_h, txt_w), baseLine = cv.getTextSize(text, cv.FONT_HERSHEY_SIMPLEX, 1, 3)

    start_point = (start_point[0] - int(txt_w / 2), start_point[1] - txt_h)
    frame = cv.putText(frame, text, start_point, cv.FONT_HERSHEY_SIMPLEX,
                       1, (255, 255, 255), 3, cv.LINE_AA)
    frame = cv.putText(frame, text, start_point, cv.FONT_HERSHEY_SIMPLEX,
                       1, (0, 0, 0), 1, cv.LINE_AA)
    return frame


def prerocess_crop(crop, tgt_shape, preprocess_type='crop'):
    height, width = tgt_shape
    crop = cv.cvtColor(crop, cv.COLOR_BGR2GRAY)
    crop = cv.cvtColor(crop, cv.COLOR_GRAY2BGR)
    ret_val, bin_crop = cv.threshold(crop, 120, 255, type=cv.THRESH_BINARY)
    image_raw = PREPROCESSING[preprocess_type](bin_crop, tgt_shape)
    img_h, img_w = image_raw.shape[0:2]
    image_raw = cv.copyMakeBorder(image_raw, 0, height - img_h,
                                  0, width - img_w, cv.BORDER_CONSTANT,
                                  None, COLOR_WHITE)
    return image_raw


def put_crop(frame, crop, start_point, end_point):
    height = end_point[1] - start_point[1]
    width = end_point[0] - start_point[0]
    crop = cv.resize(crop, (width, height))
    frame[0:height, start_point[0]:end_point[0], :] = crop
    return frame


def calculate_probability(logits):
    prob = 1
    probabilities = np.amax(logits, axis=1)
    for p in probabilities:
        prob *= p
    return prob


def build_argparser():
    parser = ArgumentParser(add_help=False)
    args = parser.add_argument_group('Options')
    args.add_argument('-h', '--help', action='help',
                      default=SUPPRESS, help='Show this help message and exit.')
    args.add_argument("-m_encoder", help="Required. Path to an .xml file with a trained encoder part of the model",
                      required=True, type=str)
    args.add_argument("-m_decoder", help="Required. Path to an .xml file with a trained decoder part of the model",
                      required=True, type=str)
    args.add_argument("--interactive", help="Optional. Enables interactive mode. In this mode images are read from the web-camera.",
                      action='store_true', default=False)
    args.add_argument("-i", "--input", help="Optional. Path to a folder with images or path to an image files",
                      required=False, type=str)
    args.add_argument("-o", "--output_file",
                      help="Optional. Path to file where to store output. If not mentioned, result will be stored"
                      "in the console.",
                      type=str)
    args.add_argument("--vocab_path", help="Required. Path to vocab file to construct meaningful phrase",
                      type=str, required=True)
    args.add_argument("--max_formula_len",
                      help="Optional. Defines maximum length of the formula (number of tokens to decode)",
                      default="128", type=int)
    args.add_argument("--conf_thresh", help="Optional. Probability threshold to trat model prediction as meaningful",
                      default=CONFIDENCE_THRESH, type=float)
    args.add_argument("-d", "--device",
                      help="Optional. Specify the target device to infer on; CPU, GPU, FPGA, HDDL or MYRIAD is "
                           "acceptable. Sample will look for a suitable plugin for device specified. Default value is CPU",
                      default="CPU", type=str)
    args.add_argument('--preprocessing_type', choices=PREPROCESSING.keys(),
                      help="Optional. Type of the preprocessing", default='crop')
    args.add_argument('-pc', '--perf_counts',
                      action='store_true', default=False)
    args.add_argument('--imgs_layer', help='Optional. Encoder input name for images. See README for details.',
                      default='imgs')
    args.add_argument('--row_enc_out_layer', help='Optional. Encoder output key for row_enc_out. See README for details.',
                      default='row_enc_out')
    args.add_argument('--hidden_layer', help='Optional. Encoder output key for hidden. See README for details.',
                      default='hidden')
    args.add_argument('--context_layer', help='Optional. Encoder output key for context. See README for details.',
                      default='context')
    args.add_argument('--init_0_layer', help='Optional. Encoder output key for init_0. See README for details.',
                      default='init_0')
    args.add_argument('--dec_st_c_layer', help='Optional. Decoder input key for dec_st_c. See README for details.',
                      default='dec_st_c')
    args.add_argument('--dec_st_h_layer', help='Optional. Decoder input key for dec_st_h. See README for details.',
                      default='dec_st_h')
    args.add_argument('--dec_st_c_t_layer', help='Optional. Decoder output key for dec_st_c_t. See README for details.',
                      default='dec_st_c_t')
    args.add_argument('--dec_st_h_t_layer', help='Optional. Decoder output key for dec_st_h_t. See README for details.',
                      default='dec_st_h_t')
    args.add_argument('--output_layer', help='Optional. Decoder output key for output. See README for details.',
                      default='output')
    args.add_argument('--output_prev_layer', help='Optional. Decoder input key for output_prev. See README for details.',
                      default='output_prev')
    args.add_argument('--logit_layer', help='Optional. Decoder output key for logit. See README for details.',
                      default='logit')
    args.add_argument('--tgt_layer', help='Optional. Decoder input key for tgt. See README for details.',
                      default='tgt')
    return parser


def main():
    log.basicConfig(format="[ %(levelname)s ] %(message)s",
                    level=log.INFO, stream=sys.stdout)

    log.info("Starting inference")
    args = build_argparser().parse_args()
    demo = Demo(args)
    if not args.interactive:
        for rec in tqdm(demo.images_list):
            image = rec['img']
            logits, targets = demo.infer_sync(image)
            prob = calculate_probability(logits)
            log.info("Confidence score is %s", prob)
            if prob >= args.conf_thresh:
                phrase = demo.vocab.construct_phrase(targets)
                if args.output_file:
                    with open(args.output_file, 'a') as output_file:
                        output_file.write(rec['img_name'] + '\t' + phrase + '\n')
                else:
                    with tempfile.NamedTemporaryFile(mode='wb') as temp_file:
                        sympy.preview(f'$${phrase}$$', viewer='file',
                                      filename=f"{temp_file.name}.png", euler=False, dvioptions=['-D', '450'])
                        print("Image name: {}\nFormula: {}\n".format(rec['img_name'], phrase))
                        rendered_formula = cv.imread(f"{temp_file.name}.png")
                        cv.imshow("Predicted formula", rendered_formula)
                        cv.waitKey(0)
    else:

        capture = create_videocapture()
        *_, height, width = demo.encoder.input_info['imgs'].input_data.shape
        aspect_ratio = height / width
        start_point, end_point = create_input_window((height, width), aspect_ratio)
        prev_text = ''
        while True:
            ret, frame = capture.read()
            bin_crop = get_crop(frame, start_point, end_point)
            model_input = prerocess_crop(bin_crop, (height, width))
            frame = put_crop(frame, model_input, start_point, end_point)
            model_res = demo.infer_async(model_input)
            if not model_res:
                phrase = prev_text
            else:
                logits, targets = model_res
                prob = calculate_probability(logits)
                log.info("Confidence score is %s", prob)
                if prob >= args.conf_thresh:
                    log.info("Prediction updated")
                    phrase = demo.vocab.construct_phrase(targets)
                else:
                    log.info("Confidence score is low, prediction is not complete")
                    phrase = '...'
            frame = put_text(frame, start_point, phrase)
            prev_text = phrase
            frame = draw_rectangle(frame, start_point, end_point)
            cv.imshow('Press Q to quit.', frame)
            key = cv.waitKey(1) & 0xFF
            if key == ord('q'):
                break
            elif key == ord('o'):
                start_point, end_point = resize_window("decrease", start_point, end_point, aspect_ratio)
            elif key == ord('p'):
                start_point, end_point = resize_window("increase", start_point, end_point, aspect_ratio)

        capture.release()
        cv.destroyAllWindows()

    log.info("This demo is an API example, for any performance measurements please use the dedicated benchmark_app tool "
             "from the openVINO toolkit\n")


if __name__ == '__main__':
    sys.exit(main() or 0)
