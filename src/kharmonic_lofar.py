from torch.autograd import Variable
import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision
import torchvision.transforms as transforms
import numpy as np
import h5py
import torch.fft
from datetime import datetime
import logging
import sys
import time
# For Saving loss to DB
import sqlite3
from sqlite3 import Error

# Note: use_cuda=True is set in lofar_tools.py, so make sure to change it
# if you change it in this script as well
from lofar_tools import *
from lofar_models import *
from lofar_tools import mydevice
# Train autoencoder and k-harmonic mean clustering using LOFAR data

# Some pre-amble variables
datestring = datetime.today().strftime('%Y-%m-%d-%H.%M.%S')
loss_db = f"loss_{datestring}.db"
logfile = f"log_{datestring}.log"

# Set up log
logging.basicConfig(
  level=logging.DEBUG,
  format='%(asctime)s %(levelname)-8s %(message)s',
  datefmt='%Y-%m-%d %H:%M:%S',
  handlers=[
    logging.FileHandler(logfile),
    logging.StreamHandler(sys.stdout)    # Specifically using log to avoid output on PG as that's buffered
  ]
)
log = logging.getLogger()

# (try to) use a GPU for computation?
#use_cuda=True
#if use_cuda and torch.cuda.is_available():
#  mydevice=torch.device('cuda')
#else:
#  mydevice=torch.device('cpu')

# Temporary hold-in value
total_bl = 406870 # Total number of useable baselines

#torch.manual_seed(69)
default_batch=96 # no. of baselines per iter, batch size determined by how many patches are created
num_epochs=10 # total epochs
Niter=int(total_bl/default_batch) # how many minibatches are considered for an epoch
Nadmm=10 # Inner optimization iterations (ADMM)
save_model=True
load_model=False

# scan directory to get valid datasets
# file names have to match the SAP ids in the sap_list
#file_list,sap_list=get_fileSAP('/media/sarod')
file_list,sap_list=get_fileSAP('C:\\LOFAR\\')
# or ../../drive/My Drive/Colab Notebooks/

Lt=16#32 # latent dimensions in time/frequency axes (1D CNN)
L=256-(2*Lt)#256 # latent dimension in real space
Kc=10 # K-harmonic clusters
Khp=4 # order of K harmonic mean 1/|| ||^p norm
alpha=0.01 # loss+alpha*cluster_loss
beta=0.01 # loss+beta*cluster_similarity (penalty)
gamma=0.01 # loss+gamma*augmentation_loss
rho=1 # ADMM rho

# reconstruction ICA
use_rica=True
rica_lambda=0.01 # scale for L1 penalty

# patch size of images
patch_size=128

num_in_channels=4 # real,imag XX,YY

# harmonic scales to use (sin,cos)(scale*u, scale*v) and so on
# these can be regarded as l,m sky coordinate distance where sources can be present
harmonic_scales=torch.tensor([1e-4, 1e-3, 1e-2, 1e-1]).to(mydevice)

# for 128x128 patches
net=AutoEncoderCNN2(latent_dim=L,channels=num_in_channels,harmonic_scales=harmonic_scales,rica=use_rica).to(mydevice)
# 1D autoencoders
netT=AutoEncoder1DCNN(latent_dim=Lt,channels=num_in_channels,harmonic_scales=harmonic_scales,rica=use_rica).to(mydevice)
netF=AutoEncoder1DCNN(latent_dim=Lt,channels=num_in_channels,harmonic_scales=harmonic_scales,rica=use_rica).to(mydevice)
# Kharmonic model
mod=Kmeans(latent_dim=(L+Lt+Lt),K=Kc,p=Khp).to(mydevice)

if load_model:
  checkpoint=torch.load('./net.model',map_location=mydevice)
  net.load_state_dict(checkpoint['model_state_dict'])
  net.train()
  checkpoint=torch.load('./khm.model',map_location=mydevice)
  mod.load_state_dict(checkpoint['model_state_dict'])
  mod.train()
  checkpoint=torch.load('./netT.model',map_location=mydevice)
  netT.load_state_dict(checkpoint['model_state_dict'])
  netT.train()
  checkpoint=torch.load('./netF.model',map_location=mydevice)
  netF.load_state_dict(checkpoint['model_state_dict'])
  netF.train()

# Set up loss database connection
db_items = [
  'epoch INTEGER',
  'iter INTEGER',
  'admm INTEGER',
  'loss0 FLOAT',
  'loss1 FLOAT',
  'loss2 FLOAT',
  'loss3 FLOAT',
  'kdist FLOAT',
  'augLoss FLOAT',
  'clusLoss FLOAT',
]

if use_rica:
  db_items.append('rica FLOAT')

try:
  conn = sqlite3.connect(loss_db)
  cur = conn.cursor()
  sql = f"create table if not exists lossTable ({(',').join(db_items)})"
  cur.execute(sql)
  conn.commit()
except Error as e:
  log.error(e)


import torch.optim as optim
from lbfgsnew import LBFGSNew # custom optimizer
criterion=nn.MSELoss(reduction='sum')
# start with empty parameter list
params=list()
params.extend(list(net.parameters()))
#params.extend(list(netT.parameters()))
#params.extend(list(netF.parameters()))
#params.extend(list(mod.parameters()))

optimizer=optim.Adam(params, lr=0.0001) # 0.001
#optimizer = LBFGSNew(params, history_size=7, max_iter=4, line_search_fn=True,batch_mode=True)

############################################################
# Augmented loss function
def augmented_loss(mu,batch_per_bline,batch_size):
 # process each 'batches_per_bline' rows of mu
 # total rows : batches_per_bline x batch_size
 loss=torch.Tensor(torch.zeros(1)).to(mydevice)
 for ck in range(batch_size):
   Z=mu[ck*batch_per_bline:(ck+1)*batch_per_bline,:]
   prod=torch.Tensor(torch.zeros(1)).to(mydevice)
   for ci in range(batch_per_bline):
     zi=Z[ci,:]/(torch.norm(Z[ci,:])+1e-6)
     for cj in range(ci+1,batch_per_bline):
       zj=Z[cj,:]/(torch.norm(Z[cj,:])+1e-6)
       prod=prod+torch.exp(-torch.dot(zi,zj))
   loss=loss+prod/batch_per_bline
 return loss/(batch_size*batch_per_bline)
############################################################


# train network
for epoch in range(num_epochs):
  for i in range(Niter):
    tic=time.perf_counter()
    # get the inputs
    patchx,patchy,inputs,uvcoords=get_data_minibatch(file_list,sap_list,batch_size=default_batch,patch_size=patch_size,normalize_data=True,num_channels=num_in_channels,uvdist=True)
    # wrap them in variable
    x=Variable(inputs).to(mydevice)
    uv=Variable(uvcoords).to(mydevice)
    (nbatch,nchan,nx,ny)=inputs.shape 
    # nbatch = patchx x patchy x default_batch
    # i.e., one baseline (per polarization, real,imag) will create patchx x patchy batches
    batch_per_bline=patchx*patchy

    # List of loss tuples for the last batch of ADMM iterations
    loss_tuples = []

    # Lagrange multipliers
    y1=torch.zeros(x.numel(),requires_grad=False).to(mydevice)
    y2=torch.zeros(x.numel(),requires_grad=False).to(mydevice)
    y3=torch.zeros(x.numel(),requires_grad=False).to(mydevice)
    for admm in range(Nadmm):
      def closure():
        if torch.is_grad_enabled():
         optimizer.zero_grad()
        x1,mu=net(x,uv)
        # residual
        x11=(x-x1)/2
        # pass through 1D CNN
        iy1=torch.flatten(x11,start_dim=2,end_dim=3)
        yyT,yyTmu=netT(iy1,uv)
        # reshape 1D outputs 
        x2=yyT.view_as(x11)

        iy2=torch.flatten(torch.transpose(x11,2,3),start_dim=2,end_dim=3)
        yyF,yyFmu=netF(iy2,uv)
        # reshape 1D outputs 
        x3=torch.transpose(yyF.view_as(x11),2,3)

        # full reconstruction
        xrecon=x1+x2+x3
 
        # normalize all losses by number of dimensions of the tensor input
        # total reconstruction loss
        loss0=(criterion(xrecon,x))/(x.numel())
        # individual losses for each AE
        loss1=(torch.dot(y1,(x-x1).view(-1))+rho/2*criterion(x,x1))/(x.numel())
        loss2=(torch.dot(y2,(x11-x2).view(-1))+rho/2*criterion(x11,x2))/(x.numel())
        loss3=(torch.dot(y3,(x11-x3).view(-1))+rho/2*criterion(x11,x3))/(x.numel())
        Mu=torch.cat((mu,yyTmu,yyFmu),1)

        kdist=alpha*mod.clustering_error(Mu)
        clus_sim=beta*mod.cluster_similarity()
        augmentation_loss=gamma*augmented_loss(Mu,batch_per_bline,default_batch)

        loss=loss0+loss1+loss2+loss3+kdist+augmentation_loss+clus_sim
        # RICA loss
        if use_rica:
          # use a differntiable approximation for L1 loss
          rica_loss=rica_lambda*(torch.sum(torch.log(torch.cosh(mu)))/mu.numel()
              +torch.sum(torch.log(torch.cosh(yyTmu)))/yyTmu.numel()
              +torch.sum(torch.log(torch.cosh(yyFmu)))/yyFmu.numel())
          loss += rica_loss

        if loss.requires_grad:
          loss.backward(retain_graph=True)
          # each output line contains:
          # epoch batch admm total_loss loss_AE1 loss_AE2 loss_AE3 loss_KHarmonic loss_augmentation loss_similarity loss_rica
          if use_rica:
            loss_tuple = (epoch,i,admm,loss0.data.item(),loss1.data.item(),loss2.data.item(),loss3.data.item(),kdist.data.item(),augmentation_loss.data.item(),clus_sim.data.item(),rica_loss.data.item())
          else:
            loss_tuple = (epoch,i,admm,loss0.data.item(),loss1.data.item(),loss2.data.item(),loss3.data.item(),kdist.data.item(),augmentation_loss.data.item(),clus_sim.data.item())
          log.info(' '.join([str(l) for l in loss_tuple]))  # Log tuple
          loss_tuples.append(loss_tuple)  # Add to collection (we only send to DB every iteration)
        return loss

      #update parameters
      optimizer.step(closure)
      # update Lagrange multipliers
      with torch.no_grad():
        x1,_=net(x,uv)
        x11=(x-x1)/2
        iy1=torch.flatten(x11,start_dim=2,end_dim=3)
        yyT,_=netT(iy1,uv)
        # reshape 1D outputs 
        x2=yyT.view_as(x11)

        iy2=torch.flatten(torch.transpose(x11,2,3),start_dim=2,end_dim=3)
        yyF,_=netF(iy2,uv)
        # reshape 1D outputs 
        x3=torch.transpose(yyF.view_as(x11),2,3)

        y1=y1+rho*(x-x1).view(-1)
        y2=y2+rho*(x11-x2).view(-1)
        y3=y3+rho*(x11-x3).view(-1)
        #print("%d %f %f %f"%(admm,torch.norm(y1),torch.norm(y2),torch.norm(y3)))
    # Record last set of ADMM interations in the loss DB
    sql = f"INSERT INTO lossTable VALUES({','.join(['?']*len(db_items))});"
    cur.executemany(sql,loss_tuples)
    conn.commit()
    toc=time.perf_counter()
    log.info(f"Iteration {i} took {toc-tic:0.4f} seconds.")
  
  # free unused memory
  if use_cuda:
     del x,x1,x11,x2,x3,iy1,iy2,yyT,yyF,y1,y2,y3
     torch.cuda.empty_cache()

if save_model:
  torch.save({
    'model_state_dict':net.state_dict()
  },'net.model')
  torch.save({
    'model_state_dict':mod.state_dict()
  },'khm.model')
  torch.save({
      'model_state_dict':netT.state_dict()
  },'netT.model')
  torch.save({
      'model_state_dict':netF.state_dict()
  },'netF.model')
