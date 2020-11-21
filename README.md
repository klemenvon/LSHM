# LSHM : LOFAR System Health Management
We combine an autoencoder with k-harmonic clustering to learn features. We combine [Deep K-means](https://arxiv.org/abs/1806.10069) and [K Harmonic means](https://www.hpl.hp.com/techreports/2000/HPL-2000-137.html) to implement deep-K-Harmonic means clustering.

Files included are:

``` lofar_models.py ``` : Methods to read LOFAR H5 data and Autoencoder models.

``` kharmonic_lofar.py ``` : Train K-harmonic autoencoders (in real and Fourier space) as well as perform clustering in latent space.

``` evaluate_clustering.py ``` : Load trained models and print clustering results for given dataset.

``` lbfgsnew.py ``` : Improved LBFGS optimizer.


<img src="arch.png" alt="Architecture of the full system" width="900"/>


The above image shows the two autoencoders that extract latent space representations for the real and Fourier space spectrograms.

