"""
A implementation of a Style Generative Adversarial Network 2 (StyleGAN2) designed for 256x256 greyscale images.
References:
https://pytorch.org/tutorials/beginner/dcgan_faces_tutorial.html
https://github.com/indiradutta/DC_GAN
https://arxiv.org/abs/1511.06434
https://github.com/NVlabs/stylegan3
https://arxiv.org/pdf/1812.04948 
"""


import torch
import torch.nn as nn
import torch.optim as optim
from torchvision import utils
import numpy as np
import torch.nn.functional as F
import math


class FullyConnectedLayer(nn.Module):
    """
    A flexible fully connected layer with various customisation options.
    Can be used in the mapping network and other parts of StyleGAN2.
    
    Args:
        in_features (int): Num input features.
        out_features (int): Num output features.
        bias (bool, optional): Include bias term? Defaults to True.
        activation (str, optional): Activation function to use. Defaults to 'linear'.
        weight_init (str): Weight initialisation method. Defaults to 'xavier'.
        dropout (float): Dropout rate. Defaults to 0.0.
        batch_norm (bool, optional): Use batch normalisation? Defaults to False.
        layer_norm (bool, optional): Use layer normalisation? Defaults to False.
    """
    def __init__(self, 
                 in_features,
                 out_features,
                 bias=True, 
                 activation='linear', 
                 weight_init='xavier',
                 dropout=0.0, 
                 batch_norm=False,
                 layer_norm=False
                ):
        super().__init__()
        self.in_features = in_features
        self.out_features = out_features
        self.activation = activation
        self.dropout = nn.Dropout(dropout) if dropout > 0 else None
        self.batch_norm = nn.BatchNorm1d(out_features) if batch_norm else None
        self.layer_norm = nn.LayerNorm(out_features) if layer_norm else None
        
        # Initialise weights and biases
        self.weight = nn.Parameter(torch.Tensor(out_features, in_features))
        if bias:
            self.bias = nn.Parameter(torch.Tensor(out_features))
        else:
            self.register_parameter('bias', None)
        
        self.reset_parameters(weight_init)
        self.act_fn = self.get_activation_fn(activation)

    def reset_parameters(self, weight_init):
        """Initialise the weights using the specified method."""
        if weight_init == 'xavier':
            nn.init.xavier_uniform_(self.weight)
        elif weight_init == 'kaiming':
            nn.init.kaiming_uniform_(self.weight, nonlinearity='relu')
        elif weight_init == 'orthogonal':
            nn.init.orthogonal_(self.weight)
        else:
            raise ValueError(f"Unsupported weight initialisation: {weight_init}")
        
        if self.bias is not None:
            fan_in, _ = nn.init._calculate_fan_in_and_fan_out(self.weight)
            bound = 1 / math.sqrt(fan_in)
            nn.init.uniform_(self.bias, -bound, bound)

    def get_activation_fn(self, activation):
        """Return the specified activation function."""
        if activation == 'relu':
            return F.relu
        elif activation == 'leaky_relu':
            return lambda x: F.leaky_relu(x, negative_slope=0.2)
        elif activation == 'elu':
            return F.elu
        elif activation == 'gelu':
            return F.gelu
        elif activation == 'swish':
            return lambda x: x * torch.sigmoid(x)
        elif activation == 'linear':
            return lambda x: x
        else:
            raise ValueError(f"Unsupported activation function: {activation}")

    def forward(self, x):
        """Forward pass of the fully connected layer."""
        x = F.linear(x, self.weight, self.bias)
        
        if self.batch_norm:
            x = self.batch_norm(x)
        if self.layer_norm:
            x = self.layer_norm(x)
        
        x = self.act_fn(x)
        
        if self.dropout:
            x = self.dropout(x)
        
        return x


class MappingNetwork(nn.Module):
    """
    Conditional Mapping Network for StyleGAN2.
    
    Network to map input latent code z and a label to intermediate latent space w.
    W used to control styles at each layer of synthesis network.

    Args:
        z_dim (int): Dim of input latent code z.
        w_dim (int): Dim of intermediate latent code w.
        num_layers (int): Num layers in mapping network.
        label_dim (int): Dim of label embedding.
        dropout (float, optional): Dropout rate. Defaults to 0.1.
    """
    def __init__(self,
                 z_dim,         # Dimension of input latent code z
                 w_dim,         # Dimension of intermediate latent code w
                 num_layers,    # Number of layers in mapping network
                 label_dim,     # Dimension of label embedding
                 dropout=0.1    # Dropout rate
                ):
        super().__init__()
        
        # Label embedding
        self.label_embedding = nn.Embedding(label_dim, z_dim)
        
        layers = []
        for i in range(num_layers):
            layers.append(FullyConnectedLayer(
                z_dim if i == 0 else w_dim,
                w_dim,
                activation='leaky_relu',
                weight_init='kaiming',
                dropout=dropout,
                batch_norm=True
            ))
        self.net = nn.Sequential(*layers)

    def forward(self, z, labels):
        """
        Transform the input latent code z and labels to the intermediate latent code w.
        """
        embedded_labels = self.label_embedding(labels)
        z_prime = z + embedded_labels  # Combine latent and label information
        return self.net(z_prime)


class NoiseInjection(nn.Module):
    """
    Noise Injection module for StyleGAN2.
    
    This module adds learnable per-pixel noise to the output of convolutional layers
    in the generator. It helps in generating fine details and stochastic variations
    in the created images.

    Args:
        channels (int): Number of input channels.
    """
    def __init__(self, 
                 channels
                ):
        super().__init__()
        # Create a learnable parameter for scaling the noise
        # One scaling factor per channel
        self.weight = nn.Parameter(torch.zeros(1, channels, 1, 1))
    
    def forward(self, x, noise=None):
        """Apply noise injection to the input tensor."""
        if noise is None:
            # Generate random noise if not provided
            batch, _, height, width = x.shape
            noise = torch.randn(batch, 1, height, width, device=x.device, dtype=x.dtype)
        
        # Scale the noise by the learned weight and add it to the input
        return x + self.weight * noise


class ModulatedConv2d(nn.Module):
    """
    Modulated Convolution layer for StyleGAN2.
    
    Applies style-based modulation to convolution weights.

    Args:
        in_channels (int): Num input channels.
        out_channels (int): Num output channels.
        kernel_size (int): Size of the conv kernel.
        style_dim (int): Dim of style vector.
        demodulate (bool, optional): Use demodulation? Defaults to True.
        up (int, optional): Upsampling factor. Defaults to 1.
        down (int, optional): Downsampling factor. Defaults to 1.
        padding (int, optional): Padding for conv. Defaults to 0.
    """
    def __init__(self,
                 in_channels,
                 out_channels,
                 kernel_size,
                 style_dim,
                 demodulate=True,
                 up=1,
                 down=1,
                 padding=0
                ):
        super().__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.kernel_size = kernel_size
        self.demodulate = demodulate
        self.up = up
        self.down = down
        self.padding = padding

        # Scaling factor for weight init
        self.scale = 1 / math.sqrt(in_channels * kernel_size ** 2)
        # Learnable conv weights
        self.weight = nn.Parameter(torch.randn(out_channels, in_channels, kernel_size, kernel_size))
        # Linear layer for style modulation
        self.modulation = nn.Linear(style_dim, in_channels, bias=False)
        # Noise injection layer
        self.noise_injection = NoiseInjection(out_channels)

    def forward(self, x, style, noise):
        """
        Forward pass of modulated convolution layer.
        
        Args:
            x (Tensor): Input feature map
            style (Tensor): Style vector
            noise (Tensor, optional): Noise tensor for injection
        
        Returns:
            Tensor: Output feature map after modulated convolution and noise injection
        """
        batch, in_channels, height, width = x.shape

        # Style Modulation
        # Transform style vector to match input channels
        style = self.modulation(style).view(batch, in_channels, 1, 1)
        # Scale weights and multiply by style
        weight = self.scale * self.weight.unsqueeze(0) * style.unsqueeze(1)

        # Demodulation
        if self.demodulate:
            # Calc demodulation factor
            demod = torch.rsqrt(weight.pow(2).sum([2, 3, 4]) + 1e-8)
            # Apply demodulation to weights
            weight = weight * demod.view(batch, self.out_channels, 1, 1, 1)

        # Reshape weight for conv
        # Combine batch and out_channels dimensions
        weight = weight.view(
            batch * self.out_channels, in_channels, self.kernel_size, self.kernel_size
        )

        # Stride and padding for up/downsampling
        stride = (1 / self.up) if self.up > 1 else self.down
        if isinstance(stride, float):
            stride = int(1 / stride)
        padding = self.kernel_size // 2 if self.padding == 0 else self.padding

        # Perform conv
        # Reshape input to combine batch and in_channels
        x = x.reshape(1, batch * in_channels, height, width)
        # Apply grouped conv (1 group per batch item)
        out = F.conv2d(x, weight, padding=padding, stride=stride, groups=batch)
        # Reshape output to original batch size
        out = out.view(batch, self.out_channels, out.size(2), out.size(3))
        
        # Inject noise
        out = self.noise_injection(out, noise)

        return out

 