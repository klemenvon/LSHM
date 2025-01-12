from torch.autograd import Variable
import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision
import torchvision.transforms as transforms
import numpy as np

# Debugging import
import logging
log = logging.getLogger()
# This file contains various models used

########################################################
class AutoEncoderCNN2(nn.Module):
    # AE CNN 
    def __init__(self,input_dim=(128,128),channels=4,latent_dim=256,k=3,s=2,harmonic_scales=None,rica=False):
        """
        input_dim: (int,int) dimensions of the 2D input image
        channels: (int) number of channels in the input image
        latent_dim: (int) dimensions of the (1,N) latent dimension
        k: (int) kernel size
        s: (int) kernel stride
        harmonic_scales: (torch.tensor) scaled multipliers for the UV input
        rica: (bool) use reconstruction ICA?
        """
        # harmonic_scales: Tensor 1xK of scales, to be used as
        # (sin,cos)(scale*u,scale*v) for each scale in scales
        # rica: if true, use reconstruction ICA, z = W s
        # z: original latent, s: sparse latent, W: basis matrix
        # encoder: sigma(W_1 z) -> use L1 constraint
        # decoder: sigma(W_2^T sigma(W_1 z)) -> pass on to other layers
        # note we keep W1 and W2 different

        super(AutoEncoderCNN2,self).__init__()
        self.in_dim = input_dim
        self.k = k              # Kernel size
        self.s = s              # Stride
        self.p = k//2           # Padding
        if self.k == 4:
          self.p = 1            # 'Legacy' support
        log.debug(f"Padding 2D AE = {self.p}")
        self.rica=rica
        self.latent_dim=latent_dim
        # scale factors for harmonics of u,v coords
        self.harmonic_scales=harmonic_scales
        # Variable to be filled on first run
        self.inner_shape = None
        # harmonic dim: H x 2(u,v) x 2(cos,sin), H from above
        self.harmonic_dim=(self.harmonic_scales.size()[0])*2*2
        # Encoder layers
        # 128x128 -> 64x64
        self.conv0=nn.Conv2d(channels, 8,self.k, stride=self.s, padding=self.p)# in channels chan, out 8 chan, kernel 4x4
        # 64x64 -> 32x32                                                                                           # Linear layers to operate on u,v coordinate harmonics
        self.conv1=nn.Conv2d(8, 12,self.k, stride=self.s, padding=self.p)# in channels 8, out 12 chan, kernel 4x4                self.fcuv1=nn.Linear(self.harmonic_dim,self.harmonic_dim)
        # 32x32 -> 16x16                                                                                           self.fcuv3=nn.Linear(self.harmonic_dim,self.harmonic_dim)
        self.conv2=nn.Conv2d(12, 24,self.k, stride=self.s,  padding=self.p)# in 12 chan, out 24 chan, kernel 4x4                 # 2x2x192=768
        # 16x16 -> 8x8                                                                                             self.fc1=nn.Linear(768+self.harmonic_dim,self.latent_dim)
        self.conv3=nn.Conv2d(24, 48,self.k, stride=self.s,  padding=self.p)# in 24 chan, out 48 chan, kernel 4x4                 if self.rica:
        # 8x8 -> 4x4                                                                                                 self.fc2in=nn.Linear(self.latent_dim,self.latent_dim)
        self.conv4=nn.Conv2d(48, 96,self.k, stride=self.s,  padding=self.p)# in 48 chan, out 96 chan, kernel 4x4                   self.fc2out=nn.Linear(self.latent_dim,self.latent_dim)
        # 4x4 -> 2x2
        self.conv5=nn.Conv2d(96, 192,self.k, stride=self.s,  padding=self.p)# in 96 chan, out 192 chan, kernel 4x4               self.fc3=nn.Linear(self.latent_dim+self.harmonic_dim,768)
        # Linear layers to operate on u,v coordinate harmonics
        self.fcuv1=nn.Linear(self.harmonic_dim,self.harmonic_dim)
        self.fcuv3=nn.Linear(self.harmonic_dim,self.harmonic_dim)
        # 2x2x192=768
        # Caclulate the resulting inner dimensions
        h,w = net_shape(input_dim[0],input_dim[1],self.k,self.s,self.p,depth=5)
        self.in_shape = (h,w)
        self.fc_amount = h*w*192
        self.fc1=nn.Linear(self.fc_amount+self.harmonic_dim,self.latent_dim)
        if self.rica:
          self.fc2in=nn.Linear(self.latent_dim,self.latent_dim)
          self.fc2out=nn.Linear(self.latent_dim,self.latent_dim)

        self.fc3=nn.Linear(self.latent_dim+self.harmonic_dim,self.fc_amount)
        # Decoder layers
        self.tconv0=nn.ConvTranspose2d(192,96,self.k,stride=self.s,padding=self.p,output_padding=1)
        self.tconv1=nn.ConvTranspose2d(96,48,self.k,stride=self.s,padding=self.p,output_padding=1)
        self.tconv2=nn.ConvTranspose2d(48,24,self.k,stride=self.s,padding=self.p,output_padding=1)
        self.tconv3=nn.ConvTranspose2d(24,12,self.k,stride=self.s,padding=self.p,output_padding=1)
        self.tconv4=nn.ConvTranspose2d(12,8,self.k,stride=self.s,padding=self.p,output_padding=1)
        self.tconv5=nn.ConvTranspose2d(8,channels,self.k,stride=self.s,padding=self.p,output_padding=1)

    def forward(self,x,uv):
        uv=torch.kron(self.harmonic_scales,uv)
        uv=torch.cat((torch.sin(uv),torch.cos(uv)),dim=1)
        uv=torch.flatten(uv,start_dim=1)
        mu=self.encode(x,uv)
        if not self.rica:
          return self.decode(mu,uv),mu
        else:
          mu=F.elu(self.fc2in(mu))
          muprime=F.elu(self.fc2out(mu))
          return self.decode(muprime,uv),mu

    def encode(self,x,uv):
        #In  1,4,128,128
        x=F.elu(self.conv0(x)) # 1,8,64,64
        x=F.elu(self.conv1(x)) # 1,12,32,32
        x=F.elu(self.conv2(x)) # 1,24,16,16
        x=F.elu(self.conv3(x)) # 1,48,8,8
        x=F.elu(self.conv4(x)) # 1,96,4,4
        x=F.elu(self.conv5(x)) # 1,192,2,2
        if not self.inner_shape:
          self.inner_shape = list(x.shape)
          self.inner_shape[0] = -1
          self.inner_shape = tuple(self.inner_shape)
          # Sanity debug to see that the calculated dimensions match observed
          #log.debug(f"Shape of inner tensor {self.inner_shape}")
          #log.debug(f"Calculated (w,h) of inner: {self.in_shape}")
        x=torch.flatten(x,start_dim=1) # 1,192*2*2=768
        uv=F.elu(self.fcuv1(uv))
        # combine uv harmonics
        x=torch.cat((x,uv),dim=1)
        x=F.elu(self.fc1(x)) # 1,latent_dim
        return x # 1,latent_dim

    def decode(self,z,uv):
        # In z: 1,latent_dim
        # harmonic input
        uv=F.elu(self.fcuv3(uv))
        z=torch.cat((z,uv),dim=1)
        x=self.fc3(z) # 1,768
        if not self.inner_shape:
          log.error("Decoder run before encoder. If you reach this you're going out of project scope.")
        x=torch.reshape(x,self.inner_shape) # 1,192,2,2
        x=F.elu(self.tconv0(x)) # 1,96,4,4
        x=F.elu(self.tconv1(x)) # 1,48,8,8
        x=F.elu(self.tconv2(x)) # 1,24,16,16
        x=F.elu(self.tconv3(x)) # 1,12,32,32
        x=F.elu(self.tconv4(x)) # 1,8,64,64
        x=self.tconv5(x) # 1,channels,128,128
        return x # 1,channels,128,128


########################################################
class AutoEncoder1DCNN(nn.Module):
    # 1 dimensional AE CNN 
    def __init__(self,input_dim=16384,channels=3,latent_dim=128,k=4,s=4,harmonic_scales=None,rica=False):
        """
        input_dim: (int) size of the 1D array to be input
        channels: (int) number of channels in the input array
        latent_dim: (int) dimensions of the latent space
        k: (int) kernel size
        s: (int) stride
        harmonic_scales: (torch.tensor) scaled multipliers for the UV input
        rica: (bool) use reconstruction ICA?
        """
        super(AutoEncoder1DCNN,self).__init__()
        self.in_dim = input_dim
        self.k = k
        self.s = s
        self.p = k//2
        if self.k == 4:
          self.p = 1
        #log.debug(f"Padding 1D AE = {self.p}")
        assert self.k >= self.s, "Kernel needs to be larger than stride!!"
        self.rica=rica
        self.latent_dim=latent_dim
        # scale factors for harmonics of u,v coords
        self.harmonic_scales=harmonic_scales
        # Variable to be filled in on first run
        self.inner_shape = None
        # harmonic dim: H x 2(u,v) x 2(cos,sin), H from above
        self.harmonic_dim=(self.harmonic_scales.size()[0])*2*2
        # all dimensions below are vectorized values
        ## 128^2x 1  -> 64^2x 1
        #self.conv0=nn.Conv1d(channels, 8, 4, stride=4, padding=1)# in channels chan, out 8 chan, kernel 4x4
        ## 64^2x1 -> 32^2x1
        #self.conv1=nn.Conv1d(8, 12, 4, stride=4, padding=1)# in channels 8, out 12 chan, kernel 4x4
        ## 32^2x1 -> 16^2x1
        #self.conv2=nn.Conv1d(12, 24, 4, stride=4,  padding=1)# in 12 chan, out 24 chan, kernel 4x4
        ## 16^2x1 -> 8^2x1
        #self.conv3=nn.Conv1d(24, 48, 4, stride=4,  padding=1)# in 24 chan, out 48 chan, kernel 4x4
        ## 8^2x1 -> 4^2x1
        #self.conv4=nn.Conv1d(48, 96, 4, stride=4,  padding=1)# in 48 chan, out 96 chan, kernel 4x4
        ## 4^2x1 -> 2^2x1
        #self.conv5=nn.Conv1d(96, 192, 4, stride=4,  padding=1)# in 96 chan, out 192 chan, kernel 4x4
        
        # 128^2x 1  -> 64^2x 1
        self.conv0=nn.Conv1d(channels, 8,self.k, stride=self.s, padding=self.p)# in channels chan, out 8 chan, kernel 4x4
        # 64^2x1 -> 32^2x1
        self.conv1=nn.Conv1d(8, 12,self.k, stride=self.s, padding=self.p)# in channels 8, out 12 chan, kernel 4x4
        # 32^2x1 -> 16^2x1
        self.conv2=nn.Conv1d(12, 24,self.k, stride=self.s,  padding=self.p)# in 12 chan, out 24 chan, kernel 4x4
        # 16^2x1 -> 8^2x1
        self.conv3=nn.Conv1d(24, 48,self.k, stride=self.s,  padding=self.p)# in 24 chan, out 48 chan, kernel 4x4
        # 8^2x1 -> 4^2x1
        self.conv4=nn.Conv1d(48, 96,self.k, stride=self.s,  padding=self.p)# in 48 chan, out 96 chan, kernel 4x4
        # 4^2x1 -> 2^2x1
        self.conv5=nn.Conv1d(96, 192,self.k, stride=self.s,  padding=self.p)# in 96 chan, out 192 chan, kernel 4x4

        # Linear layers to operate on u,v coordinate harmonics
        self.fcuv1=nn.Linear(self.harmonic_dim,self.harmonic_dim)
        self.fcuv3=nn.Linear(self.harmonic_dim,self.harmonic_dim)
        # 2^2x192=768
        # Calculate the resulting inner dimensions
        self.in_shape,_ = net_shape(input_dim,0,self.k,self.s,self.p,depth=5)
        self.fc_amount = self.in_shape*192
        self.fc1=nn.Linear(self.fc_amount+self.harmonic_dim,self.latent_dim)
        if self.rica:
          self.fc2in=nn.Linear(self.latent_dim,self.latent_dim)
          self.fc2out=nn.Linear(self.latent_dim,self.latent_dim)

        self.fc3=nn.Linear(self.latent_dim+self.harmonic_dim,self.fc_amount)
        # output_padding is added to match the input sizes
        #self.tconv0=nn.ConvTranspose1d(192,96,4,stride=4,padding=0,output_padding=0)
        #self.tconv1=nn.ConvTranspose1d(96,48,4,stride=4,padding=0,output_padding=0)
        #self.tconv2=nn.ConvTranspose1d(48,24,4,stride=4,padding=0,output_padding=0)
        #self.tconv3=nn.ConvTranspose1d(24,12,4,stride=4,padding=0,output_padding=0)
        #self.tconv4=nn.ConvTranspose1d(12,8,4,stride=4,padding=0,output_padding=0)
        #self.tconv5=nn.ConvTranspose1d(8,channels,4,stride=4,padding=0,output_padding=0)
        # Decoder layers
        self.tconv0=nn.ConvTranspose1d(192,96,self.k,stride=self.s,padding=0,output_padding=0)
        self.tconv1=nn.ConvTranspose1d(96,48,self.k,stride=self.s,padding=0,output_padding=0)
        self.tconv2=nn.ConvTranspose1d(48,24,self.k,stride=self.s,padding=0,output_padding=0)
        self.tconv3=nn.ConvTranspose1d(24,12,self.k,stride=self.s,padding=0,output_padding=0)
        self.tconv4=nn.ConvTranspose1d(12,8,self.k,stride=self.s,padding=0,output_padding=0)
        self.tconv5=nn.ConvTranspose1d(8,channels,self.k,stride=self.s,padding=0,output_padding=0)

    def forward(self, x, uv):
        uv=torch.kron(self.harmonic_scales,uv)
        uv=torch.cat((torch.sin(uv),torch.cos(uv)),dim=1)
        uv=torch.flatten(uv,start_dim=1)
        mu=self.encode(x,uv)
        if not self.rica:
          return self.decode(mu), mu
        else:
          mu=F.elu(self.fc2in(mu))
          muprime=F.elu(self.fc2out(mu))
          return self.decode(muprime,uv),mu

    def encode(self, x, uv):
        #In  1,4,128^2
        x=F.elu(self.conv0(x)) # 1,8,64^2
        x=F.elu(self.conv1(x)) # 1,12,32^2
        x=F.elu(self.conv2(x)) # 1,24,16^2
        x=F.elu(self.conv3(x)) # 1,48,8^2
        x=F.elu(self.conv4(x)) # 1,96,4^2
        x=F.elu(self.conv5(x)) # 1,192,2^2
        if not self.inner_shape:
          self.inner_shape = list(x.shape)
          self.inner_shape[0] = -1
          self.inner_shape = tuple(self.inner_shape)
          # Sanity check debug
          #log.debug(f"Shape of inner tensor 1D {self.inner_shape}")
          #log.debug(f"Calculated inner shape 1D {self.in_shape}")
        x=torch.flatten(x,start_dim=1) # 1,192*2*2=768
        uv=F.elu(self.fcuv1(uv))
        # combine uv harmonics
        x=torch.cat((x,uv),dim=1)
        x=F.elu(self.fc1(x)) # 1,latent_dim
        return x # 1,latent_dim

    def decode(self, z, uv):
        # In 1,latent_dim
        # harmonic input
        uv=F.elu(self.fcuv3(uv))
        z=torch.cat((z,uv),dim=1)
        x=self.fc3(z) # 1,768
        if not self.inner_shape:
          log.error("Decoder run before encoder. If you reach this you're going out of project scope.")
        x=torch.reshape(x,self.inner_shape) # 1,192,2^2
        x=F.elu(self.tconv0(x)) # 1,96,4^2
        x=F.elu(self.tconv1(x)) # 1,48,8^2
        x=F.elu(self.tconv2(x)) # 1,24,16^2
        x=F.elu(self.tconv3(x)) # 1,12,32^2
        x=F.elu(self.tconv4(x)) # 1,8,64^2
        x=self.tconv5(x) # 1,channels,128^2
        return x # 1,channels,128^2


########################################################
#### K harmonic means module
class Kmeans(nn.Module):
  def __init__(self,latent_dim=128,K=10,p=2):
     super(Kmeans,self).__init__()
     self.latent_dim=latent_dim
     self.K=K
     self.p=p # K harmonic mean order 1/|| ||^p
     self.EPS=1e-9# epsilon to avoid 1/0 cases
     # cluster centroids
     self.M=torch.nn.Parameter(torch.rand(self.K,self.latent_dim),requires_grad=True)

  def forward(self,X):
     # calculate distance of each X from cluster centroids
     (nbatch,_)=X.shape
     loss=0
     for nb in range(nbatch):
       # calculate harmonic mean for x := K/ sum_k (1/||x-m_k||^p)
       ek=0
       for ck in range(self.K):
         ek=ek+1.0/(torch.pow(torch.linalg.norm(self.M[ck,:]-X[nb,:],2),self.p)+self.EPS)
       loss=loss+self.K/(ek+self.EPS)
     return loss/(nbatch*self.K*self.latent_dim)

  def clustering_error(self,X):
    return self.forward(X)

  def cluster_similarity(self):
     # use contrastive loss variant
     # for each row k, denominator=exp(zk^T zk/||zk||^2)
     # numerator = sum_l,l\ne k exp(zk^T zl / ||zk|| ||zl||)
     loss=0
     # take outer product between each rows
     for ci in range(self.K):
       mnrm=torch.linalg.norm(self.M[ci,:],2)
       # denominator is actually=1
       denominator=torch.exp(torch.dot(self.M[ci,:],self.M[ci,:])/(mnrm*mnrm+self.EPS))
       numerator=0
       for cj in range(self.K):
        if cj!=ci:
          numerator=numerator+torch.exp(torch.dot(self.M[ci,:],self.M[cj,:])/(mnrm*torch.linalg.norm(self.M[cj,:],2)+self.EPS))
       loss=loss+(numerator/(denominator+self.EPS))
     return loss/(self.K*self.latent_dim)

  def offline_update(self,X):
      # update cluster centroids using recursive formula
      # Eq (7.1-7.5) of B. Zhang - generalized K-harmonic means
      (nbatch,_)=X.shape
      alpha=torch.zeros(nbatch)
      Q=torch.zeros(nbatch,self.K)
      q=torch.zeros(self.K)
      P=torch.zeros(nbatch,self.K)
      # indices i=1..nbatch, k or j=1..K
      for ci in range(nbatch):
        # alpha_i := 1/ (sum_k (1/||x_i-m_k||^p))^2
        ek=0
        for ck in range(self.K):
          ek=ek+1.0/(torch.pow(torch.linalg.norm(self.M[ck,:]-X[ci,:],2),self.p)+self.EPS)
        alpha[ci]=1.0/(ek**2+self.EPS)
        # Q_ij = alpha_i/ ||x_i-m_j||^(p+2)
        for ck in range(self.K):
          Q[ci,ck]=alpha[ci]/(torch.pow(torch.linlag.norm(self.M[ck,:]-X[ci,:],2),self.p+2)+self.EPS)
      # q_j = sum_i Q_ij
      for ck in range(self.K):
          q[ck]=torch.sum(Q[:,ck])
      # P_ij = Q_ij/q_j
      for ci in range(nbatch):
        for ck in range(self.K):
          P[ci,ck]=Q[ci,ck]/q[ck]
      # M_j = sum_i P_ij x_i
      for ck in range(self.K):
        self.M[ck,:]=0
        for ci in range(nbatch):
          self.M[ck,:]+=P[ci,ck]*X[ci,:]
      del P,Q,q,alpha
########################################################

def net_shape(w,h,k,s,p,depth=0):
  """
  w: (int) width of input
  h: (int) height of input
  k: (int) kernel size (assumed square)
  s: (int) kernel stride
  p: (int) padding
  depth: (int > 0) how many repeated layers to calculate for?
  """
  new_w = (w - k + 2*p)//s + 1
  new_h = (h - k + 2*p)//s + 1
  if depth == 0:
    return new_w, new_h
  else:
    return net_shape(new_w,new_h,k,s,p,depth-1)