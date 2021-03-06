import numpy as np
import tensorflow as tf

import layers


def parameter_efficient(in_channels=1, out_channels=2, start_filters=64, input_side_length=256, depth=4, res_blocks=2, filter_size=3, sparse_labels=True, batch_size=1, activation="cReLU", batch_norm=True):
    """
    Creates the graph for the parameter efficient variant of the U-Net and sets up the appropriate input and output placeholder.

    Parameters
    ----------
    in_channels: int
        The depth of the input.
    out_channels: int
        The depth of number of classes of the output.
    start_filters : int
        The number of filters in the first convolution.
    input_side_length: int
        The side length of the square input.
    depth: int
        The depth of the U-part of the network. This is equal to the number of max-pooling layers.
    res_blocks: int
        The number of residual blocks in between max-pooling layers on the down-path and in between up-convolutions on the up-path.
    filter_size: int
        The width and height of the filter. The receptive field.
    sparse_labels: bool
        If true, the labels are integers, one integer per pixel, denoting the class that that pixel belongs to. If false, labels are one-hot encoded.
    batch_size: int
        The training batch size.
    activation: string
        Either "ReLU" for the standard ReLU activation or "cReLU" for the concatenated ReLU activation function.
    batch_norm: bool
        Whether to use batch normalization or not.

    Returns
    -------
    inputs : TF tensor
        The network input.
    logits: TF tensor
        The network output before SoftMax.
    ground_truth: TF tensor
        The desired output from the ground truth.
    keep_prob: TF float
        The TF variable holding the keep probability for drop out layers.
    training_bool: TF bool
        The TF variable holding the boolean value, which switches batch normalization to training or inference mode.    
    """

    activation = str.lower(activation)
    if activation not in ["relu", "crelu"]:
        raise ValueError("activation must be \"ReLU\" or \"cReLU\".")

    pool_size = 2

    # Define inputs and helper functions #

    with tf.variable_scope('inputs'):
        inputs = tf.placeholder(tf.float32, shape=(batch_size, input_side_length, input_side_length, in_channels), name='inputs')
        if sparse_labels:
            ground_truth = tf.placeholder(tf.int32, shape=(batch_size, input_side_length, input_side_length), name='labels')
        else:
            ground_truth = tf.placeholder(tf.float32, shape=(batch_size, input_side_length, input_side_length, out_channels), name='labels')
        keep_prob = tf.placeholder(tf.float32, shape=[], name='keep_prob')
        training = tf.placeholder(tf.bool, shape=[], name="training")

        network_input = tf.transpose(inputs, perm=[0, 3, 1, 2])

    # [conv -> conv -> max pool -> drop out] + parameter updates
    def step_down(name, input_, filter_size=3, res_blocks=2, keep_prob=1., training=False):

        with tf.variable_scope(name):
            
            with tf.variable_scope("res_block_0"):
                conv_out, tiled_input = layers.res_block(input_, filter_size, channel_multiplier=2, depthwise_multiplier=2, convolutions=2, training=training, activation=activation, batch_norm=batch_norm, data_format="NCHW")
            
            for i in xrange(1, res_blocks):
                with tf.variable_scope("res_block_" + str(i)):
                    conv_out = layers.res_block(conv_out, filter_size, channel_multiplier=1, depthwise_multiplier=2, convolutions=2, training=training, activation=activation, batch_norm=batch_norm, data_format="NCHW")
            
            conv_out = conv_out + tiled_input

            pool_out = layers.max_pool(conv_out, pool_size, data_format="NCHW")
            
            bottom_out = layers.dropout(pool_out, keep_prob)
            side_out = layers.dropout(conv_out, keep_prob)

        return bottom_out, side_out

    # parameter updates + [upconv and concat -> drop out -> conv -> conv]
    def step_up(name, bottom_input, side_input, filter_size=3, res_blocks=2, keep_prob=1., training=False):

        with tf.variable_scope(name):
            added_input = layers.upconv_add_block(bottom_input, side_input, data_format="NCHW")

            conv_out = added_input
            for i in xrange(res_blocks):
                with tf.variable_scope("res_block_" + str(i)):
                    conv_out = layers.res_block(conv_out, filter_size, channel_multiplier=1, depthwise_multiplier=2, convolutions=2, training=training, activation=activation, batch_norm=batch_norm, data_format="NCHW")
            
            result = layers.dropout(conv_out, keep_prob)

        return result

    # Build the network #

    with tf.variable_scope('contracting'):

        outputs = []

        with tf.variable_scope("step_0"):

            # Conv 1
            in_filters = in_channels
            out_filters = start_filters

            stddev = np.sqrt(2. / (filter_size**2 * in_filters))
            w = layers.weight_variable([filter_size, filter_size, in_filters, out_filters], stddev=stddev, name="weights")

            out_ = tf.nn.conv2d(network_input, w, [1, 1, 1, 1], padding="SAME", data_format="NCHW")
            out_ = out_ + layers.bias_variable([out_filters, 1, 1], name='biases')

            # Batch Norm 1
            if batch_norm:
                out_ = tf.layers.batch_normalization(out_, axis=1, momentum=0.999, center=True, scale=True, training=training, trainable=True, name="batch_norm", fused=True)

            in_filters = out_filters

            # concatenated ReLU
            if activation == "crelu":
                out_ = tf.concat([out_, -out_], axis=1)
                in_filters = 2 * in_filters
            out_ = tf.nn.relu(out_)

            # Conv 2
            stddev = np.sqrt(2. / (filter_size**2 * in_filters))
            w = layers.weight_variable([filter_size, filter_size, in_filters, out_filters], stddev=stddev, name="weights")

            out_ = tf.nn.conv2d(out_, w, [1, 1, 1, 1], padding="SAME", data_format="NCHW")
            out_ = out_ + layers.bias_variable([out_filters, 1, 1], name='biases')

            # Res Block 1
            conv_out = layers.res_block(out_, filter_size, channel_multiplier=1, depthwise_multiplier=2, convolutions=2, training=training, activation=activation, batch_norm=batch_norm, data_format="NCHW")

            pool_out = layers.max_pool(conv_out, pool_size, data_format="NCHW")
            
            bottom_out = layers.dropout(pool_out, keep_prob)
            side_out = layers.dropout(conv_out, keep_prob)

            outputs.append(side_out)

        # Build contracting path
        for i in xrange(1, depth):
            bottom_out, side_out = step_down('step_' + str(i), bottom_out, filter_size=filter_size, res_blocks=res_blocks, keep_prob=keep_prob, training=training)
            outputs.append(side_out)

    # Bottom [conv -> conv]
    with tf.variable_scope('step_' + str(depth)):

        with tf.variable_scope("res_block_0"):
            conv_out, tiled_input = layers.res_block(bottom_out, filter_size, channel_multiplier=2, depthwise_multiplier=2, convolutions=2, training=training, activation=activation, batch_norm=batch_norm, data_format="NCHW")
        for i in xrange(1, res_blocks):
            with tf.variable_scope("res_block_" + str(i)):
                conv_out = layers.res_block(conv_out, filter_size, channel_multiplier=1, depthwise_multiplier=2, convolutions=2, training=training, activation=activation, batch_norm=batch_norm, data_format="NCHW")
        
        conv_out = conv_out + tiled_input
        current_tensor = layers.dropout(conv_out, keep_prob)

    with tf.variable_scope('expanding'):

        # Set initial parameter
        outputs.reverse()

        # Build expanding path
        for i in xrange(depth):
            current_tensor = step_up('step_' + str(depth + i + 1), current_tensor, outputs[i], filter_size=filter_size, res_blocks=res_blocks, keep_prob=keep_prob, training=training)
 
    # Last layer is a 1x1 convolution to get the predictions
    # We don't want an activation function for this one (softmax will be applied later), so we're doing it manually
    in_filters = current_tensor.shape.as_list()[1]
    stddev = np.sqrt(2. / in_filters)

    with tf.variable_scope('classification'):

        w = layers.weight_variable([1, 1, in_filters, out_channels], stddev, name='weights')
        b = layers.bias_variable([out_channels, 1, 1], name='biases')

        conv = tf.nn.conv2d(current_tensor, w, strides=[1, 1, 1, 1], padding="SAME", data_format="NCHW", name='conv')
        logits = conv + b

        logits = tf.transpose(logits, perm=[0, 2, 3, 1])

    return inputs, logits, ground_truth, keep_prob, training


def unet(in_channels=1, out_channels=2, start_filters=64, side_length=572, depth=4, convolutions=2, filter_size=3, sparse_labels=True, batch_size=1):
    """
    Creates the graph for the standard U-Net and sets up the appropriate input and output placeholder.

    Parameters
    ----------
    in_channels: int
        The depth of the input.
    out_channels: int
        The depth of number of classes of the output.
    start_filters : int
        The number of filters in the first convolution.
    side_length: int
        The side length of the square input.
    depth: int
        The depth of the U-part of the network. This is equal to the number of max-pooling layers.
    convolutions: int
        The number of convolutions in between max-pooling layers on the down-path and in between up-convolutions on the up-path.
    filter_size: int
        The width and height of the filter. The receptive field.
    sparse_labels: bool
        If true, the labels are integers, one integer per pixel, denoting the class that that pixel belongs to. If false, labels are one-hot encoded.
    batch_size: int
        The training batch size.

    Returns
    -------
    inputs : TF tensor
        The network input.
    logits: TF tensor
        The network output before SoftMax.
    ground_truth: TF tensor
        The desired output from the ground truth.
    keep_prob: TF float
        The TF variable holding the keep probability for drop out layers.  
    """

    pool_size = 2
    padding = "SAME"

    # Define inputs and helper functions #
    with tf.variable_scope('inputs'):
        inputs = tf.placeholder(tf.float32, shape=(batch_size, side_length, side_length, in_channels), name='inputs')
        if sparse_labels:
            ground_truth = tf.placeholder(tf.int32, shape=(batch_size, side_length, side_length), name='labels')
        else:
            ground_truth = tf.placeholder(tf.float32, shape=(batch_size, side_length, side_length, out_channels), name='labels')
        keep_prob = tf.placeholder(tf.float32, shape=[], name='keep_prob')

        network_input = tf.transpose(inputs, perm=[0, 3, 1, 2])

    # [conv -> conv -> max pool -> drop out] + parameter updates
    def step_down(name, _input):

        with tf.variable_scope(name):
            conv_out = layers.conv_block(_input, filter_size, channel_multiplier=2, convolutions=convolutions, padding=padding, data_format="NCHW")
            pool_out = layers.max_pool(conv_out, pool_size, data_format="NCHW")
            result = layers.dropout(pool_out, keep_prob)

        return result, conv_out

    # parameter updates + [upconv and concat -> drop out -> conv -> conv]
    def step_up(name, bottom_input, side_input):

        with tf.variable_scope(name):
            concat_out = layers.upconv_concat_block(bottom_input, side_input, data_format="NCHW")
            drop_out = layers.dropout(concat_out, keep_prob)
            result = layers.conv_block(drop_out, filter_size, channel_multiplier=0.5, convolutions=convolutions, padding=padding, data_format="NCHW")

        return result

    # Build the network #

    with tf.variable_scope('contracting'):

        # Set initial parameters
        outputs = []

        # Build contracting path
        with tf.variable_scope("step_0"):
            conv_out = layers.conv_block(network_input, filter_size, out_filters=start_filters, convolutions=convolutions, padding=padding, data_format="NCHW")
            pool_out = layers.max_pool(conv_out, pool_size, data_format="NCHW")
            current_tensor = layers.dropout(pool_out, keep_prob)
            outputs.append(conv_out)

        for i in xrange(1, depth):
            current_tensor, conv_out = step_down("step_" + str(i), current_tensor)
            outputs.append(conv_out)

    # Bottom [conv -> conv]
    with tf.variable_scope("step_" + str(depth)):
        current_tensor = layers.conv_block(current_tensor, filter_size, channel_multiplier=2, convolutions=convolutions, padding=padding, data_format="NCHW")

    with tf.variable_scope("expanding"):

        # Set initial parameter
        outputs.reverse()

        # Build expanding path
        for i in xrange(depth):
            current_tensor = step_up("step_" + str(depth + i + 1), current_tensor, outputs[i])

    # Last layer is a 1x1 convolution to get the predictions
    # We don't want an activation function for this one (softmax will be applied later), so we're doing it manually
    in_filters = current_tensor.shape.as_list()[1]
    stddev = np.sqrt(2. / in_filters)

    with tf.variable_scope("classification"):

        weight = layers.weight_variable([1, 1, in_filters, out_channels], stddev, name="weights")
        bias = layers.bias_variable([out_channels, 1, 1], name="biases")

        conv = tf.nn.conv2d(current_tensor, weight, strides=[1, 1, 1, 1], padding="VALID", name="conv", data_format="NCHW")
        logits = conv + bias

        logits = tf.transpose(logits, perm=[0, 2, 3, 1])

    return inputs, logits, ground_truth, keep_prob


def get_output_side_length(side_length, depth, convolutions, filter_size, pool_size):
    """
    Computes the output side length for a standard U-Net without padded convolutions.

    Parameters
    ----------
    side_length: int
        The side length of the square input.
    depth: int
        The depth of the U-part of the network. This is equal to the number of max-pooling layers.
    convolutions: int
        The number of convolutions in between max-pooling layers on the down-path and in between up-convolutions on the up-path.
    filter_size: int
        The width and height of the filter. The receptive field.
    pool_size: int
        The width and height of the filter. The receptive field.
    batch_size: int
        The training batch size.
    padded_convolutions: bool
        Whether to pad the input to keep the side length constant through convolutional layers or not.
        If no padding is used, the side length decreases with every convolution.

    Returns
    -------
    inputs : TF tensor
        The network input.
    logits: TF tensor
        The network output before SoftMax.
    ground_truth: TF tensor
        The desired output from the ground truth.
    keep_prob: TF float
        The TF variable holding the keep probability for drop out layers.  
    """

    for i in xrange(depth - 1):

        for j in xrange(convolutions):
            side_length -= (filter_size - 1)
            if side_length < 0:
                raise ValueError("Input side length too small. Side length < 0 in contracting path after {} max pooling layers plus {} convolution.".format(i, j + 1))

        if (side_length % pool_size) != 0:
            raise ValueError("problem with input side length. Side length not divisible by pool size {}. Side length is {} before max pooling layer {}.".format(pool_size, side_length, i + 1))
        else:
            side_length /= pool_size

    for j in xrange(convolutions):
        side_length -= (filter_size - 1)
        if side_length < 0:
            raise ValueError("Input side length too small. Side length < 0 at bottom layer after {} convolution.".format(j + 1))

    for i in xrange(depth - 1):
        side_length *= pool_size
        side_length -= convolutions * (filter_size - 1)

    return side_length
