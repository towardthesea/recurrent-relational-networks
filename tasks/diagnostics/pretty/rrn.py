import os

import matplotlib
import tensorflow as tf
from tensorboard.plugins.image.summary import pb as ipb
from tensorboard.plugins.scalar.summary import pb as spb
from tensorflow.contrib import layers
from tensorflow.python.data import Dataset
from tensorflow.contrib.rnn import LSTMCell

import util
from message_passing import message_passing
from model import Model
from tasks.diagnostics.pretty.data import PrettyClevr, fig2array
import numpy as np

matplotlib.use('Agg')
import matplotlib.pyplot as plt


class PrettyRRN(Model):
    batch_size = 512
    revision = os.environ.get('REVISION')
    message = os.environ.get('MESSAGE')
    n_objects = 8
    data = PrettyClevr()
    n_steps = 8
    n_hidden = 128
    devices = util.get_devices()

    def __init__(self):
        super().__init__()
        self.name = "%s %s" % (self.revision, self.message)

        print("Building graph...")
        self.session = tf.Session(config=tf.ConfigProto(allow_soft_placement=False))
        self.global_step = tf.Variable(initial_value=0, trainable=False)
        self.optimizer = tf.train.AdamOptimizer(1e-4)

        iterator = self._iterator(self.data)
        n_nodes = 8
        n_anchors_targets = len(self.data.i2s)

        def mlp(x, scope, n_hid=self.n_hidden, n_out=self.n_hidden):
            with tf.variable_scope(scope):
                for i in range(3):
                    x = layers.fully_connected(x, n_hid)
                return layers.fully_connected(x, n_out, activation_fn=None)

        def forward(img, anchors, n_jumps, targets, positions, colors, markers):
            """
            :param img: (bs, 128, 128, 3)
            :param anchors: (bs,)
            :param n_jumps: (bs,)
            :param targets: (bs,)
            :param positions: (bs, 8, 2)
            :param colors: (bs, 8)
            """
            bs = self.batch_size // len(self.devices)
            edges = [(i, j) for i in range(n_nodes) for j in range(n_nodes)]
            edges = tf.constant([(i + (b * n_nodes), j + (b * n_nodes)) for b in range(bs) for i, j in edges], tf.int32)

            """
            x = ((1. - tf.to_float(img) / 255.) - 0.5)  # (bs, h, w, 3)
            with tf.variable_scope('encoder'):
                for i in range(5):
                    x = layers.conv2d(x, num_outputs=self.n_hidden, kernel_size=3, stride=2)  # (bs, 4, 4, 128)
            x = tf.reshape(x, (bs * n_nodes, self.n_hidden))
            """

            positions = tf.reshape(positions, (bs * n_nodes, 2))
            colors = tf.reshape(tf.one_hot(colors, 8), (bs * n_nodes, 8))
            markers = tf.reshape(tf.one_hot(markers, 8), (bs * n_nodes, 8))
            x = tf.concat([positions, colors, markers], axis=1)
            # x = mlp(x, "encoder")

            question = tf.concat([tf.one_hot(anchors, n_anchors_targets), tf.one_hot(n_jumps, self.n_objects)], axis=1)  # (bs, 24)
            question = tf.reshape(tf.tile(tf.expand_dims(question, 1), [1, n_nodes, 1]), [bs * n_nodes, 24])
            # question = mlp(question, "q")
            n_edges = tf.shape(edges)[0]
            edge_features = tf.zeros((n_edges, 1))

            x = mlp(tf.concat([x, question], axis=1), "pre")

            with tf.variable_scope('steps'):
                outputs = []
                losses = []
                x0 = x
                lstm_cell = LSTMCell(self.n_hidden)
                state = lstm_cell.zero_state(n_nodes * bs, tf.float32)
                for step in range(self.n_steps):
                    x = message_passing(x, edges, edge_features, lambda x: mlp(x, 'message-fn'))
                    x = mlp(tf.concat([x, x0], axis=1), 'post')
                    x = layers.batch_norm(x, scope='bn')
                    x, state = lstm_cell(x, state)

                    logits = x
                    logits = tf.reshape(logits, (bs, n_nodes, self.n_hidden))
                    logits = tf.reduce_sum(logits, axis=1)
                    logits = mlp(logits, "out", n_out=n_anchors_targets)

                    out = tf.argmax(logits, axis=1)
                    outputs.append(out)
                    loss = tf.reduce_mean(tf.nn.sparse_softmax_cross_entropy_with_logits(labels=targets, logits=logits) / tf.log(2.))
                    losses.append(loss)

                    tf.get_variable_scope().reuse_variables()

            return losses, outputs

        self.org_img, positions, colors, markers, self.anchors, self.n_jumps, self.targets = iterator.get_next()
        losses, outputs = util.batch_parallel(forward, self.devices, img=self.org_img, anchors=self.anchors, n_jumps=self.n_jumps, targets=self.targets, positions=positions, colors=colors, markers=markers)
        losses = tf.reduce_mean(losses)
        self.outputs = tf.concat(outputs, axis=1)  # (splits, steps, bs)

        self.loss = tf.reduce_mean(losses)
        tf.summary.scalar('loss', self.loss)

        gvs = self.optimizer.compute_gradients(self.loss, colocate_gradients_with_ops=True)
        for g, v in gvs:
            tf.summary.histogram("grads/" + v.name, g)
            tf.summary.histogram("vars/" + v.name, v)
            tf.summary.histogram("g_ratio/" + v.name, g / (v + 1e-8))

        gvs = [(tf.clip_by_value(g, -1., 1.), v) for g, v in gvs]
        update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
        with tf.control_dependencies(update_ops):
            self.train_step = self.optimizer.apply_gradients(gvs, global_step=self.global_step)

        self.session.run(tf.global_variables_initializer())
        self.saver = tf.train.Saver()
        util.print_vars(tf.trainable_variables())

        tensorboard_dir = os.environ.get('TENSORBOARD_DIR') or '/tmp/tensorboard'
        self.train_writer = tf.summary.FileWriter(tensorboard_dir + '/pretty/%s/train/%s' % (self.revision, self.name), self.session.graph)
        self.test_writer = tf.summary.FileWriter(tensorboard_dir + '/pretty/%s/test/%s' % (self.revision, self.name), self.session.graph)
        self.summaries = tf.summary.merge_all()

    def train_batch(self):
        _, loss = self.session.run([self.train_step, self.loss])
        return loss

    def val_batch(self):
        loss, summaries, step, img, anchors, jumps, targets, outputs = self.session.run([self.loss, self.summaries, self.global_step, self.org_img, self.anchors, self.n_jumps, self.targets, self.outputs])
        self._write_summaries(self.test_writer, summaries, img, anchors, jumps, targets, outputs, step)
        return loss

    def save(self, name):
        self.saver.save(self.session, name)

    def load(self, name):
        print("Loading %s..." % name)
        self.saver.restore(self.session, name)

    def _iterator(self, data):
        return Dataset.from_generator(
            data.sample_generator,
            data.output_types(),
            data.output_shapes()
        ).batch(self.batch_size).prefetch(1).make_one_shot_iterator()

    def _render(self, img, anchor, jump, target, outputs):
        remap = {'blue': 'b', 'green': 'g', 'red': 'r', 'cyan': 'c', 'magenta': 'm', 'yellow': 'y', 'black': 'k', 'gray': 'a'}
        fig = plt.figure(figsize=(2.56, 2.56), frameon=False)
        plt.imshow(img)
        outs = [self.data.i2s[output[0]] for output in outputs]
        out_str = "".join([remap[o] if o in remap else o for o in outs])
        title = "%s %d %s\n%s" % (self.data.i2s[anchor], jump, self.data.i2s[target], out_str)
        plt.title(title)
        plt.xticks([])
        plt.yticks([])
        plt.tight_layout()

        return fig2array(fig)

    def _write_summaries(self, writer, summaries, img, anchors, jumps, targets, outputs, step):
        for t in range(self.n_steps):
            equal = outputs[t] == targets
            for i in range(8):
                jumps_i = jumps == i
                if any(jumps_i):
                    acc = np.mean(equal[jumps_i])
                    writer.add_summary(spb("acc/%d/%d" % (t, i), acc), step)

        imgs = self._render(img[0], int(anchors[0]), int(jumps[0]), int(targets[0]), outputs)
        img_summary = ipb("img", imgs[None])
        writer.add_summary(img_summary, step)

        writer.add_summary(summaries, step)
        writer.flush()


if __name__ == '__main__':
    m = PrettyRRN()
    print(m.train_batch())
    print(m.val_batch())