import multiprocessing as mp
import os
import pickle
import queue
from multiprocessing import Process
from multiprocessing import Process

import cv2 as cv
import numpy as np
from keras.applications.inception_resnet_v2 import preprocess_input
from tqdm import tqdm

from config import best_model, lfw_folder, img_size, channel
from utils import select_triplets


class InferenceWorker(Process):
    def __init__(self, gpuid, in_queue, out_queue, signal_queue):
        Process.__init__(self, name='ImageProcessor')

        self.gpuid = gpuid
        self.in_queue = in_queue
        self.out_queue = out_queue
        self.signal_queue = signal_queue

    def run(self):
        # set enviornment
        os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
        os.environ["CUDA_VISIBLE_DEVICES"] = str(self.gpuid)
        print("InferenceWorker init, GPU ID: {}".format(self.gpuid))

        from model import build_model

        # load models
        model = build_model()
        model.load_weights(best_model)

        while True:
            try:
                try:
                    sample = self.in_queue.get(block=False)
                except queue.Empty:
                    continue

                batch_inputs = np.empty((3, 1, img_size, img_size, channel), dtype=np.float32)

                for j, role in enumerate(['a', 'p', 'n']):
                    image_name = sample[role]
                    filename = os.path.join(lfw_folder, image_name)
                    image_bgr = cv.imread(filename)
                    image_bgr = cv.resize(image_bgr, (img_size, img_size), cv.INTER_CUBIC)
                    image_rgb = cv.cvtColor(image_bgr, cv.COLOR_BGR2RGB)
                    batch_inputs[j, 0] = preprocess_input(image_rgb)

                y_pred = model.predict([batch_inputs[0], batch_inputs[1], batch_inputs[2]])
                a = y_pred[0, 0:128]
                p = y_pred[0, 128:256]
                n = y_pred[0, 256:384]

                self.out_queue.put(
                    {'image_name_a': sample['a'], 'embedding_a': a, 'image_name_p': sample['p'], 'embedding_p': p,
                     'image_name_n': sample['n'], 'embedding_n': n})
                self.signal_queue.put(SENTINEL)

                if self.in_queue.qsize() == 0:
                    break
            except Exception as e:
                print(e)

        import keras.backend as K
        K.clear_session()
        print('InferenceWorker done, GPU ID {}'.format(self.gpuid))


class Scheduler:
    def __init__(self, gpuids, signal_queue):
        self.signal_queue = signal_queue
        manager = mp.Manager()
        self.in_queue = manager.Queue()
        self.out_queue = manager.Queue()
        self._gpuids = gpuids

        self.__init_workers()

    def __init_workers(self):
        self._workers = list()
        for gpuid in self._gpuids:
            self._workers.append(InferenceWorker(gpuid, self.in_queue, self.out_queue, self.signal_queue))

    def start(self, samples):
        # put all of image names into queue
        for sample in samples:
            self.in_queue.put(sample)

        # start the workers
        for worker in self._workers:
            worker.start()

        # wait all fo workers finish
        for worker in self._workers:
            worker.join()
        print("all of workers have been done")
        return self.out_queue


def run(gpuids, q):
    # scan all files under img_path
    samples = select_triplets('valid')

    # init scheduler
    x = Scheduler(gpuids, q)

    # start processing and wait for complete
    return x.start(samples)


SENTINEL = 1


def listener(q):
    pbar = tqdm(total=13233 // 3)
    for item in iter(q.get, None):
        pbar.update()


def inference():
    gpuids = ['0', '1', '2', '3']
    print(gpuids)

    manager = mp.Manager()
    q = manager.Queue()
    proc = mp.Process(target=listener, args=(q,))
    proc.start()

    out_queue = run(gpuids, q)
    out_list = []
    while out_queue.qsize() > 0:
        out_list.append(out_queue.get())

    with open("data/valid.p", "wb") as file:
        pickle.dump(out_list, file)

    q.put(None)
    proc.join()


if __name__ == '__main__':
    if not os.path.isfile('data/valid.p'):
        inference()
    with open('data/valid.p', 'rb') as file:
        samples = pickle.load(file)

    threshold = 0.6

    y_true_list = []
    y_pred_list = []

    for sample in tqdm(samples):
        embedding_a = sample['embedding_a']
        embedding_p = sample['embedding_p']
        embedding_n = sample['embedding_n']
        y_true_list.append(True)
        y_true_list.append(False)

        dist_1 = np.square(np.linalg.norm(embedding_a - embedding_p))
        y_pred = dist_1 <= threshold
        dist_2 = np.square(np.linalg.norm(embedding_a - embedding_n))
        y_pred = dist_2 <= threshold
        y_pred_list.append(y_pred)

    y = np.array(y_true_list).astype(np.int32)
    pred = np.array(y_pred_list).astype(np.int32)
    from sklearn import metrics

    print(y)
    print(pred)

    fpr, tpr, thresholds = metrics.roc_curve(y, pred)
    print(metrics.auc(fpr, tpr))