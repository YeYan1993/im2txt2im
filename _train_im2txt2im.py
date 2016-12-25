#! /usr/bin/python
# -*- coding: utf8 -*-




"""Generate captions for images by a given model."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import math
import os
import numpy as np
import scipy
import time
from PIL import Image
import logging
LOG_FILENAME = 'record.log'
logging.basicConfig(filename=LOG_FILENAME,
                    level=logging.DEBUG)

import tensorflow as tf
import tensorlayer as tl
from tensorlayer.prepro import *
from tensorlayer.layers import *
import nltk
from model import * #<--- Modify from Image-Captioning repo
from utils import *

images_train_dir = '/home/haodong/Workspace/image_captioning/data/mscoco/raw-data/train2014/'
images_train_list = tl.files.load_file_list(path=images_train_dir, regx='\\.jpg', printable=False)
# print(train_img_list)
images_train_list = [images_train_dir + s for s in images_train_list]
images_train_list = np.asarray(images_train_list)
# print(images_train_list)
n_images_train = len(images_train_list)
# exit()

DIR = "/home/haodong/Workspace/image_captioning"

# Directory containing model checkpoints.
CHECKPOINT_DIR = DIR + "/model/train"
# Vocabulary file generated by the preprocessing script.
VOCAB_FILE = DIR + "/data/mscoco/word_counts.txt"

save_dir = "checkpoint" # <-- store the GAN model
if not os.path.exists(save_dir):
    print("[!] Folder %s is not exist, create it." % save_dir)
    os.mkdir(save_dir)
sample_dir = "samples"  # <-- store generated images from seed
if not os.path.exists(sample_dir):
    print("[!] Folder %s is not exist, create it." % sample_dir)
    os.mkdir(sample_dir)

tf.logging.set_verbosity(tf.logging.INFO) # Enable tf.logging

image_size = 64     # image size for txt2im
vocab_size = 12000
word_embedding_size = 512   # <-- word embedding size, lstm hidden size
keep_prob = 0.7
z_dim = 100         # Noise dimension
t_dim = 128         # Text feature dimension # paper said 128
c_dim = 3           # for rgb
gf_dim = 64         # Number of conv in the first layer generator 64
df_dim = 64         # Number of conv in the first layer discriminator 64

def prepro_img(x, mode='for_image_caption'):
    if mode=='random_size_to_346':
        x = imresize(x, size=[resize_height, resize_width], interp='bilinear', mode=None)   # <-- random size to (346, 346)
    elif mode=='central_crop':
        ## see model.py: image captioning preprocess the images as below.
        # image = tf.image.resize_images(image,
        #                                size=[resize_height, resize_width],
        #                                method=tf.image.ResizeMethod.BILINEAR)
        # image = tf.image.resize_image_with_crop_or_pad(image, [image_height, image_width, 3]) <-- central crop
        # image = tf.sub(image, 0.5)
        # image = tf.mul(image, 2.0)
        # x = imresize(x, size=[resize_height, resize_width], interp='bilinear', mode=None)   # <-- random size to (346, 346)
        x = crop(x, wrg=image_width, hrg=image_height, is_random=False)                     # <-- (346, 346) to (299, 299) central crop
        x = x / (255. / 2.)
        x = x - 1.
    elif mode=='read_image':
        # x : file name
        x = scipy.misc.imread(x, mode='RGB')
    elif mode=='random_crop_to_64':
        # for txt2im
        # x = imresize(x, size=[resize_height, resize_width], interp='bilinear', mode=None)   # <-- random size to (346, 346)
        x = flip_axis(x, axis=1, is_random=True)
        # x = rotation(x, rg=5, is_random=True, fill_mode='nearest')    # <-- no rotation for im2txt
        x = crop(x, wrg=image_width, hrg=image_width, is_random=True) # <-- (346, 346) to (299, 299)
        x = imresize(x, size=[image_size, image_size], interp='bilinear', mode=None)    # <-- (346, 346) to (64, 64)
        x = x / (255. / 2.)
        x = x - 1.
    else:
        raise Exception("Unsupport mode %s" % mode)
    return x

def sample_fn(softmax_output, top_k, vocab):
    a_id = tl.nlp.sample_top(softmax_output, top_k=top_k)
    word = vocab.id_to_word(a_id)
    return a_id, word

def rnn_embed(input_seqs, is_train, reuse):
    """MY IMPLEMENTATION, same weights for the Word Embedding and RNN in the discriminator and generator.
    """
    w_init = tf.random_normal_initializer(stddev=0.02)
    # w_init = tf.constant_initializer(value=0.0)
    with tf.variable_scope("rnn", reuse=reuse):
        tl.layers.set_name_reuse(reuse)
        network = EmbeddingInputlayer(
                     inputs = input_seqs,
                     vocabulary_size = vocab_size,
                     embedding_size = word_embedding_size,
                     E_init = w_init,
                     name = 'wordembed')
        network = DynamicRNNLayer(network,
                     cell_fn = tf.nn.rnn_cell.LSTMCell,
                     n_hidden = word_embedding_size,
                     dropout = (keep_prob if is_train else None),
                     initializer = w_init,
                     sequence_length = tl.layers.retrieve_seq_length_op2(input_seqs),
                     return_last = True,
                     name = 'dynamic')

        # network = BiDynamicRNNLayer(network,
        #              cell_fn = tf.nn.rnn_cell.LSTMCell,
        #              n_hidden = word_embedding_size,
        #              dropout = (keep_prob if is_train else None),
        #              initializer = w_init,
        #              sequence_length = tl.layers.retrieve_seq_length_op2(input_seqs),
        #              return_last = True,
        #              return_last_mode = 'simple',
        #              name = 'bidynamic')

        # paper 4.1: reduce the dim of description embedding in (seperate) FC layer followed by rectification
        # network = DenseLayer(network, n_units=t_dim,
        #         act=lambda x: tl.act.lrelu(x, 0.2), W_init=w_init, name='reduce_txt/dense')
    return network

def generator_txt2img(input_z, net_rnn_embed=None, is_train=True, reuse=False):
    # IMPLEMENTATION based on : https://github.com/paarthneekhara/text-to-image/blob/master/model.py
    s = image_size
    s2, s4, s8, s16 = int(s/2), int(s/4), int(s/8), int(s/16)

    w_init = tf.random_normal_initializer(stddev=0.02)
    gamma_init = tf.random_normal_initializer(1., 0.02)

    with tf.variable_scope("generator", reuse=reuse):
        tl.layers.set_name_reuse(reuse)
        net_in = InputLayer(input_z, name='g_inputz')

        if net_rnn_embed is not None:
            # paper 4.1 : the discription embedding is first compressed using a FC layer to small dim (128), followed by leaky-Relu
            net_reduced_text = DenseLayer(net_rnn_embed, n_units=t_dim,
                    act=lambda x: tl.act.lrelu(x, 0.2),
                    W_init = w_init, name='g_reduce_text/dense')
            # paper 4.1 : and then concatenated to the noise vector z
            net_in = ConcatLayer([net_in, net_reduced_text], concat_dim=1, name='g_concat_z_seq')
        else:
            print("No text info will be used, i.e. normal DCGAN")

        net_h0 = DenseLayer(net_in, gf_dim*8*s16*s16, act=tf.identity,
                W_init=w_init, name='g_h0/dense')                  # (64, 8192)
        net_h0 = ReshapeLayer(net_h0, [-1, s16, s16, gf_dim*8], name='g_h0/reshape')
        net_h0 = BatchNormLayer(net_h0, act=tf.nn.relu, is_train=is_train,
                gamma_init=gamma_init, name='g_h0/batch_norm')

        net_h1 = DeConv2d(net_h0, gf_dim*4, (5, 5), out_size=(s8, s8), strides=(2, 2),
                padding='SAME', batch_size=batch_size, act=None, W_init=w_init, name='g_h1/decon2d')
        net_h1 = BatchNormLayer(net_h1, act=tf.nn.relu, is_train=is_train,
                gamma_init=gamma_init, name='g_h1/batch_norm')

        net_h2 = DeConv2d(net_h1, gf_dim*2, (5, 5), out_size=(s4, s4), strides=(2, 2),
                padding='SAME', batch_size=batch_size, act=None, W_init=w_init, name='g_h2/decon2d')
        net_h2 = BatchNormLayer(net_h2, act=tf.nn.relu, is_train=is_train,
                gamma_init=gamma_init, name='g_h2/batch_norm')

        net_h3 = DeConv2d(net_h2, gf_dim, (5, 5), out_size=(s2, s2), strides=(2, 2),
                padding='SAME', batch_size=batch_size, act=None, W_init=w_init, name='g_h3/decon2d')
        net_h3 = BatchNormLayer(net_h3, act=tf.nn.relu, is_train=is_train,
                gamma_init=gamma_init, name='g_h3/batch_norm')

        net_h4 = DeConv2d(net_h3, c_dim, (5, 5), out_size=(s, s), strides=(2, 2),
                padding='SAME', batch_size=batch_size, act=None, W_init=w_init, name='g_h4/decon2d')
        logits = net_h4.outputs
        # net_h4.outputs = tf.nn.sigmoid(net_h4.outputs)  # DCGAN uses tanh
        net_h4.outputs = tf.nn.tanh(net_h4.outputs)
    return net_h4, logits

def discriminator_txt2img(input_images, net_rnn_embed=None, is_train=True, reuse=False):
    # IMPLEMENTATION based on : https://github.com/paarthneekhara/text-to-image/blob/master/model.py
    #       https://github.com/reedscot/icml2016/blob/master/main_cls_int.lua
    w_init = tf.random_normal_initializer(stddev=0.02)
    gamma_init=tf.random_normal_initializer(1., 0.02)

    with tf.variable_scope("discriminator", reuse=reuse):
        tl.layers.set_name_reuse(reuse)

        net_in = InputLayer(input_images, name='d_input/images')
        net_h0 = Conv2d(net_in, df_dim, (5, 5), (2, 2), act=lambda x: tl.act.lrelu(x, 0.2),
                padding='SAME', W_init=w_init, name='d_h0/conv2d')  # (64, 32, 32, 64)

        net_h1 = Conv2d(net_h0, df_dim*2, (5, 5), (2, 2), act=None,
                padding='SAME', W_init=w_init, name='d_h1/conv2d')
        net_h1 = BatchNormLayer(net_h1, act=lambda x: tl.act.lrelu(x, 0.2),
                is_train=is_train, gamma_init=gamma_init, name='d_h1/batchnorm') # (64, 16, 16, 128)

        net_h2 = Conv2d(net_h1, df_dim*4, (5, 5), (2, 2), act=None,
                padding='SAME', W_init=w_init, name='d_h2/conv2d')
        net_h2 = BatchNormLayer(net_h2, act=lambda x: tl.act.lrelu(x, 0.2),
                is_train=is_train, gamma_init=gamma_init, name='d_h2/batchnorm')    # (64, 8, 8, 256)

        net_h3 = Conv2d(net_h2, df_dim*8, (5, 5), (2, 2), act=None,
                padding='SAME', W_init=w_init, name='d_h3/conv2d')
        net_h3 = BatchNormLayer(net_h3, act=lambda x: tl.act.lrelu(x, 0.2),
                is_train=is_train, gamma_init=gamma_init, name='d_h3/batchnorm') # (64, 4, 4, 512)  paper 4.1: when the spatial dim of the D is 4x4, we replicate the description embedding spatially and perform a depth concatenation

        if net_rnn_embed is not None:
            # paper : reduce the dim of description embedding in (seperate) FC layer followed by rectification
            net_reduced_text = DenseLayer(net_rnn_embed, n_units=t_dim,
                   act=lambda x: tl.act.lrelu(x, 0.2),
                   W_init=w_init, name='d_reduce_txt/dense')
            # net_reduced_text = net_rnn_embed  # if reduce_txt in rnn_embed
            net_reduced_text.outputs = tf.expand_dims(net_reduced_text.outputs, 1)
            net_reduced_text.outputs = tf.expand_dims(net_reduced_text.outputs, 2)
            net_reduced_text.outputs = tf.tile(net_reduced_text.outputs, [1, 4, 4, 1], name='d_tiled_embeddings')

            net_h3_concat = ConcatLayer([net_h3, net_reduced_text], concat_dim=3, name='d_h3_concat') # (64, 4, 4, 640)
            # net_h3_concat = net_h3 # no text info
            net_h3 = Conv2d(net_h3_concat, df_dim*8, (1, 1), (1, 1), padding='SAME', W_init=w_init, name='d_h3/conv2d_2')   # paper 4.1: perform 1x1 conv followed by rectification and a 4x4 conv to compute the final score from D
            net_h3 = BatchNormLayer(net_h3, act=lambda x: tl.act.lrelu(x, 0.2),
                    is_train=is_train, gamma_init=gamma_init, name='d_h3/batch_norm_2') # (64, 4, 4, 512)
        else:
            print("No text info will be used, i.e. normal DCGAN")

        net_h4 = FlattenLayer(net_h3, name='d_h4/flatten')          # (64, 8192)
        net_h4 = DenseLayer(net_h4, n_units=1, act=tf.identity,
                W_init = w_init, name='d_h4/dense')
        logits = net_h4.outputs
        net_h4.outputs = tf.nn.sigmoid(net_h4.outputs)  # (64, 1)
    return net_h4, logits

def main(_):
    # Model checkpoint file or directory containing a model checkpoint file.
    checkpoint_path = CHECKPOINT_DIR
    # Text file containing the vocabulary.
    vocab_file = VOCAB_FILE
    # File pattern or comma-separated list of file patterns of image files.

    mode = 'inference'  # <-- generating image captions
    max_caption_length = 20 # <-- the smaller the faster to generate captions
    top_k = 2           #
    # n_captions = 1      # for im2txt2im, it should be 1
    print("n_images_train: %d" % n_images_train)

    # g = tf.Graph()
    # with g.as_default():

    ## Build graph for im2txt
    # images, input_seqs, target_seqs, input_mask, input_feed = Build_Inputs(mode, input_file_pattern=None)
    images = tf.placeholder('float32', [batch_size, image_height, image_width, 3])
    input_seqs = tf.placeholder(dtype=tf.int64, shape=[batch_size, None], name='input_seqs')
    net_image_embeddings = Build_Image_Embeddings(mode, images, train_inception=False)
    net_seq_embeddings = Build_Seq_Embeddings(input_seqs)
    softmax, net_img_rnn, net_seq_rnn, state_feed = Build_Model(mode, net_image_embeddings, net_seq_embeddings, target_seqs=None, input_mask=None)

    if tf.gfile.IsDirectory(checkpoint_path):
        checkpoint_path = tf.train.latest_checkpoint(checkpoint_path)
        if not checkpoint_path:
            raise ValueError("No im2txt checkpoint file found in: %s" % checkpoint_path)

    saver = tf.train.Saver()
    def _restore_fn(sess):
        tf.logging.info("Loading model from im2txt checkpoint: %s", checkpoint_path)
        saver.restore(sess, checkpoint_path)
        tf.logging.info("Successfully loaded im2txt checkpoint: %s",
                      os.path.basename(checkpoint_path))

    restore_fn = _restore_fn

    ## Create the vocabulary.
    vocab = tl.nlp.Vocabulary(vocab_file)

    ## Build graph for txt2im.
    t_real_image = tf.placeholder('float32', [batch_size, image_size, image_size, 3], name = 'real_image')
    t_real_caption = tf.placeholder(dtype=tf.int64, shape=[batch_size, None], name='real_caption_input')     # remove if DCGAN only
    t_wrong_caption = tf.placeholder(dtype=tf.int64, shape=[batch_size, None], name='real_wrong_input')     # remove if DCGAN only
    t_z = tf.placeholder(tf.float32, [batch_size, z_dim], name='z_noise')

    net_rnn = rnn_embed(t_real_caption, is_train=True, reuse=False)    # if pre-trained
    net_fake_image, _ = generator_txt2img(t_z,
                    # net_rnn,                                       # remove if DCGAN only
                    rnn_embed(t_real_caption, is_train=False, reuse=True), # <-  disable RNN dropout in G
                    is_train=True, reuse=False)
                    # is_train=False, reuse=False)# <- disable batch norm in G
    net_d, disc_fake_image_logits = discriminator_txt2img(
                    net_fake_image.outputs,
                    net_rnn,                                       # remove if DCGAN only
                    is_train=True, reuse=False)
    _, disc_real_image_logits = discriminator_txt2img(
                    t_real_image,
                    net_rnn,                                          # remove if DCGAN only
                    is_train=True, reuse=True)
    _, disc_wrong_caption_logits = discriminator_txt2img(
                    t_real_image,
                    rnn_embed(t_wrong_caption, is_train=True, reuse=True),                                          # remove if DCGAN only
                    is_train=True, reuse=True)

    # testing inference for txt2img
    net_g, _ = generator_txt2img(t_z,
                    rnn_embed(t_real_caption, is_train=False, reuse=True), # remove if DCGAN only
                    is_train=False, reuse=True)

    ## loss for txt2im.
    d_loss1 = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(disc_real_image_logits, tf.ones_like(disc_real_image_logits)))
    d_loss2 = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(disc_wrong_caption_logits, tf.zeros_like(disc_wrong_caption_logits)))
    d_loss3 = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(disc_fake_image_logits, tf.zeros_like(disc_fake_image_logits)))

    d_loss = d_loss1 + d_loss2 + d_loss3

    # _, disc_fake_image_logits2 = discriminator_txt2img(
    #                 net_fake_image.outputs,
    #                 rnn_embed(t_real_caption, is_train=False, reuse=True), # <-  disable RNN dropout in G
    #                 is_train=False, reuse=True) # <- disable batch norm in D

    g_loss = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(disc_fake_image_logits, tf.ones_like(disc_fake_image_logits))) # real == 1, fake == 0

    net_fake_image.print_params(False)
    net_fake_image.print_layers()

    ## cost for txt2im (real = 1, fake = 0)
    lr = 0.0002
    beta1 = 0.5
    e_vars = tl.layers.get_variables_with_name('rnn', True, True)           #  remove if DCGAN only
    d_vars = tl.layers.get_variables_with_name('discriminator', True, True)
    g_vars = tl.layers.get_variables_with_name('generator', True, True)

    # d_optim = tf.train.AdamOptimizer(lr, beta1=beta1).minimize(d_loss, var_list=d_vars)# + e_vars)
    g_optim = tf.train.AdamOptimizer(lr, beta1=beta1).minimize(g_loss, var_list=g_vars )#+ e_vars)

    opt1 = tf.train.AdamOptimizer(lr, beta1=beta1)
    opt2 = tf.train.AdamOptimizer(lr/1, beta1=beta1)
    grads = tf.gradients(d_loss, d_vars + e_vars)
    # grads, _ = tf.clip_by_global_norm(grads, 10)      # Truncated grads global
    grads1 = grads[0:len(d_vars)]
    grads2 = grads[-len(e_vars):]
    # grads2, _ = = tf.clip_by_global_norm(grads2, 10)      # Truncated
    train_op1 = opt1.apply_gradients(zip(grads1, d_vars))
    train_op2 = opt2.apply_gradients(zip(grads2, e_vars))
    d_optim = tf.group(train_op1, train_op2)

    # g.finalize()

    sample_size = batch_size
    # sample_seed = np.random.uniform(low=-1, high=1, size=(sample_size, z_dim)).astype(np.float32)        # paper said normal distribution [u=0, std=1]
    sample_seed = np.random.normal(loc=0.0, scale=1.0, size=(sample_size, z_dim)).astype(np.float32)
    sample_sentence = ["a sheep standing in a open grass field",
                        "a pitcher is about to throw the ball to the batter"] * int(batch_size/2)
    # sample_sentence = captions_ids_test[0:sample_size]
    for i, sentence in enumerate(sample_sentence):
        sample_sentence[i] = [vocab.word_to_id(word) for word in nltk.tokenize.word_tokenize(sentence)]
        # sample_sentence[i] = [vocab.word_to_id(word) for word in sentence]
        print("seed: %s" % sentence)
        # print(sample_sentence[i])
    sample_sentence = tl.prepro.pad_sequences(sample_sentence, padding='post')

    # Train txt2im
    # with tf.Session(graph=g) as sess:
    with tf.Session() as sess:
        sess.run(tf.initialize_all_variables())
        ## Restore the im2txt model from checkpoint.
        restore_fn(sess)

        ## Restore the txt2im model from checkpoint
        net_e_name = os.path.join(save_dir, 'net_e.npz')
        net_g_name = os.path.join(save_dir, 'net_g.npz')
        net_d_name = os.path.join(save_dir, 'net_d.npz')
        if not os.path.exists(net_e_name):
            print("[!] Loading RNN checkpoints failed!")
        else:
            net_e_loaded_params = tl.files.load_npz(name=net_e_name)
            tl.files.assign_params(sess, net_e_loaded_params, net_rnn)
            print("[*] Loading RNN checkpoints SUCCESS!")
        if not (os.path.exists(net_g_name) and os.path.exists(net_d_name)):
            print("[!] Loading G and D checkpoints failed!")
        else:
            net_g_loaded_params = tl.files.load_npz(name=net_g_name)
            net_d_loaded_params = tl.files.load_npz(name=net_d_name)
            tl.files.assign_params(sess, net_g_loaded_params, net_g)
            tl.files.assign_params(sess, net_d_loaded_params, net_d)
            print("[*] Loading G and D checkpoints SUCCESS!")

        # exit()
        n_step = 1000000
        n_check_step = 500
        total_d_loss, total_g_loss = 0, 0
        for step in range(70501, n_step):
            start_time = time.time()

            ### im2txt : Generate Training and Captions
            idexs = get_random_int(min=0, max=n_images_train-1, number=batch_size)
            b_image_file_name = images_train_list[idexs]

            # temp_time = time.time()
            ## read a batch of images for folder
            b_images = threading_data(b_image_file_name, prepro_img, mode='read_image')
            # print('im2txt read image %f'% (time.time() - temp_time))  # <-- 0.08 for 64 imgs
            # temp_time = time.time()
            ## you may want to view the original image
            # for i, img in enumerate(b_images):
            #     scipy.misc.imsave('image_orig_%d.png' % i, img)
            ## preprocess a batch of image
            b_images = threading_data(b_images, prepro_img, mode='random_size_to_346')
            # print(b_images.shape, np.min(b_images), np.max(b_images))
            # print('im2txt random2fixsize image %f' % (time.time() - temp_time))   # <-- 0.06 for 64 imgs
            # temp_time = time.time()
            b_images_im2txt = threading_data(b_images, prepro_img, mode='central_crop')
            # print(b_images_im2txt.shape, np.min(b_images_im2txt), np.max(b_images_im2txt))
            # print('im2txt central_crop image %f' % (time.time() - temp_time))   # <-- 0.05 for 64 imgs
            # temp_time = time.time()
            ## you may want to view the original image after central crop and rescale
            # for i, img in enumerate(b_images_im2txt):
            #     scipy.misc.imsave('image_im2txt%d.png' % i, img)
            # generate captions for a batch of images
            # temp_time = time.time()

            init_state = sess.run(net_img_rnn.final_state, feed_dict={images: b_images_im2txt})
            state = np.hstack((init_state.c, init_state.h)) # (1, 1024)
            ids = [[vocab.start_id]] * batch_size

            b_sentences = [[] for _ in range(batch_size)]
            b_sentences_ids = [[] for _ in range(batch_size)]
            for _ in range(max_caption_length - 1):
                softmax_output, state = sess.run([softmax, net_seq_rnn.final_state],
                                        feed_dict={ input_seqs : ids,
                                                    state_feed : state,
                                                    })
                state = np.hstack((state.c, state.h))

                # temp_time = time.time()
                ids = []
                temp = threading_data(softmax_output, sample_fn, top_k=top_k, vocab=vocab)    # <-- not a lot faster
                i = 0
                for a_id, word in temp:
                # for i in range(batch_size):
                    # a_id = tl.nlp.sample_top(softmax_output[i], top_k=top_k)
                    # word = vocab.id_to_word(a_id)
                    b_sentences[i].append(word)
                    b_sentences_ids[i].append(int(a_id))
                    ids = ids + [[a_id]]
                    i = i + 1
                # print("im2txt sample from top k (1 step): %f" % (time.time()-temp_time)) # <-- 0.008 ~ 0.024s (x max_caption_length) for 62 img
                # temp_time = time.time()
            # print('im2txt get caption %f'% (time.time() - temp_time))   # <-- 0.xx 32 imgs
            # temp_time = time.time()
            # before cleaning caption data
            # for i, b_sentence in enumerate(b_sentences):
            #     print("%d : %s" % (i, " ".join(b_sentence)))
            #
            # for i, b_sentence_id in enumerate(b_sentences_ids):
            #     print("%d : %s" % (i, b_sentence_id))
            #
            # print("took %s seconds" % (time.time()-start_time))

            ## cleaning caption data
            b_sentences_ids = precess_sequences(b_sentences_ids, end_id=vocab.end_id, pad_val=0, is_shorten=True)
            # for i, b_sentence_id in enumerate(b_sentences_ids):
            #     print("%d : %s" % (i, b_sentence_id))
            #
            ## you may want to view the training captions
            # for i, b_sentence_id in enumerate(b_sentences_ids):
            #     print("%d : %s" % (i, [vocab.id_to_word(id) for id in b_sentence_id]) )
            #
            # print("start_id:%d end_id:%d" % (vocab.start_id, vocab.end_id))
            # print("step %d took %s seconds" % (step, time.time()-start_time))

            ## txt2im : Train GAN
            # read image    : b_images_txt2im
            # read caption  : b_sentences_ids
            # wrong caption : b_wrong_sentences_ids
            # print(b_images_im2txt.shape, np.min(b_images_im2txt), np.max(b_images_im2txt), b_images_im2txt.dtype)   # (32, 299, 299, 3) -1.0 1.0
            b_images_txt2im = threading_data(b_images, prepro_img, mode='random_crop_to_64')
            # print(b_images_txt2im.shape, np.min(b_images_txt2im), np.max(b_images_txt2im))
            # print('txt2im distort images %f'% (time.time() - temp_time))    # <-- 0.11 for 64 imgs
            # temp_time = time.time()
            # print(b_images_txt2im.shape, np.min(b_images_txt2im), np.max(b_images_txt2im), b_images_txt2im.dtype)   # (32, 64, 64, 3) -1.0 1.0
            #
            ## you may want to view the image after data augmentation
            # for i, img in enumerate(b_images_txt2im):
            #     scipy.misc.imsave('image_txt2im_%d.png' % i, img)

            b_wrong_sentences_ids = b_sentences_ids[-1:]+b_sentences_ids[:-1]   # <-- the wrong captions are the real captions shift by 1
            # for i, b_sentence_id in enumerate(b_wrong_sentences_ids):
            #     print("%d : %s" % (i, [vocab.id_to_word(id) for id in b_sentence_id]) )
            # exit()

            # b_z = np.random.uniform(low=-1, high=1, size=[batch_size, z_dim]).astype(np.float32)    # paper said normal distribution [u=0, std=1]
            b_z = np.random.normal(loc=0.0, scale=1.0, size=(sample_size, z_dim)).astype(np.float32)
            # exit()
            errD, _ = sess.run([d_loss, d_optim], feed_dict={
                            t_real_image : b_images_txt2im,
                            t_wrong_caption : b_wrong_sentences_ids,
                            t_real_caption : b_sentences_ids,
                            t_z : b_z})

            for _ in range(2):
                errG, _ = sess.run([g_loss, g_optim], feed_dict={
                                t_real_caption : b_sentences_ids,    # remove if DCGAN only
                                t_z : b_z})
            total_d_loss += errD
            total_g_loss += errG
            # print('txt2im train GAN %f'% (time.time() - temp_time)) # <-- 0.19 for 64 imgs
            # temp_time = time.time()
            print("step %d: d_loss: %.8f, g_loss: %.8f (%4.4f sec)" % (step, errD, errG, time.time()-start_time))
            # exit()
            if step != 0 and step % n_check_step == 0:
                ## Print average loss
                # print(" ** d_loss: %.8f, g_loss: %.8f: " % (total_d_loss/n_check_step, total_g_loss/n_check_step))
                log = " ** step: %d d_loss: %.8f, g_loss: %.8f: " % (step, total_d_loss/n_check_step, total_g_loss/n_check_step)
                print(log)
                logging.debug(log)
                total_d_loss, total_g_loss = 0, 0
                ## Generate a batch of image by given seeds
                img_gen, rnn_out = sess.run([net_g.outputs, net_rnn.outputs],
                                            feed_dict={
                                            t_real_caption : sample_sentence,  # remove if DCGAN only
                                            t_z : sample_seed})
                # print(img_gen.shape)        # <-- (batch_size, 64, 64, 3)
                save_images(img_gen, [8, int(batch_size/8)], '{}/train_{:02d}.png'.format(sample_dir, step))
                ## Save model to npz
                tl.files.save_npz(net_rnn.all_params, name=net_e_name, sess=sess)
                tl.files.save_npz(net_g.all_params, name=net_g_name, sess=sess)
                tl.files.save_npz(net_d.all_params, name=net_d_name, sess=sess)
                net_e_name_ = os.path.join(save_dir, 'net_e_%d.npz' % step)
                net_g_name_ = os.path.join(save_dir, 'net_g_%d.npz' % step)
                net_d_name_ = os.path.join(save_dir, 'net_d_%d.npz' % step)
                tl.files.save_npz(net_rnn.all_params, name=net_e_name_, sess=sess)
                tl.files.save_npz(net_g.all_params, name=net_g_name_, sess=sess)
                tl.files.save_npz(net_d.all_params, name=net_d_name_, sess=sess)
                print("[*] Saving txt2im checkpoints SUCCESS!")
            # exit()

if __name__ == "__main__":
  tf.app.run()
