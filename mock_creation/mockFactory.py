import numpy as np
import tensorflow as tf
import tensorflow_datasets as tfds
import optparse, math, sys, glob, os
from astropy.cosmology import Planck15
from astropy import units
from astropy.cosmology import z_at_value
from powerbox.dft import fft, ifft
from functools import reduce
import py21cmfast as p21c

# Tool for efficient mock creation. N_file specifies the tfrecords file number N for which the mocks are created.
o = optparse.OptionParser()
o.set_usage('mockFactory.py [options] [N_file]')
o.add_option('--model', dest='model', default="opt",
             help="Foreground model to be used for mock creation. Options are opt and mod.")
opts, args = o.parse_args(sys.argv[1:])

# Read light-cones from tfrecords files
def parse_function(files):
    keys_to_features = {"label":tf.io.FixedLenFeature((6),tf.float32),
                        "image":tf.io.FixedLenFeature((),tf.string),
                        "tau":tf.io.FixedLenFeature((),tf.float32),
                        "gxH":tf.io.FixedLenFeature((92),tf.float32),
                        "z":tf.io.FixedLenFeature((92),tf.float32),}
    parsed_features = tf.io.parse_example(files, keys_to_features)
    image = tf.io.decode_raw(parsed_features["image"],tf.float16)
    image = tf.reshape(image,(140,140,2350))
    return image, tf.concat([parsed_features["label"],[parsed_features["tau"]],parsed_features["gxH"],parsed_features["z"]],axis=0) #WDM,OMm,LX,E0,Tvir,Zeta,tau,gxH*92,z*92

# Save mock light-cones as a tfrecords file
def save(lightcone,labels,writer):
    LClist = tf.train.BytesList(value=[lightcone.flatten().astype(np.float16).tobytes()])
    LBlist = tf.train.FloatList(value=labels[:6])
    Taulist = tf.train.FloatList(value=[labels[6]])
    gxHlist = tf.train.FloatList(value=labels[7:99])
    Zlist = tf.train.FloatList(value=labels[99:])
    image = tf.train.Feature(bytes_list=LClist)
    label = tf.train.Feature(float_list=LBlist)
    Tau = tf.train.Feature(float_list=Taulist)
    Redshift = tf.train.Feature(float_list=Zlist)
    gxH = tf.train.Feature(float_list=gxHlist)
    LCdict={
        'image': image,
        'label': label,
        'tau': Tau,
        'z': Redshift,
        'gxH': gxH,
    }
    features = tf.train.Features(feature=LCdict)
    labeled_data = tf.train.Example(features=features)
    writer.write(labeled_data.SerializeToString())

# Read output from 21cmSense and sort by frequency
if opts.model=="opt":
    files = glob.glob("calcfiles/opt_mocks/SKA1_Lowtrack_6.0hr_opt_0.*_LargeHII_Pk_Ts1_Tb9_nf0.52_v2.npz")
elif opts.model=="mod":
    files = glob.glob("calcfiles/mod_mocks/SKA1_Lowtrack_6.0hr_mod_0.*_LargeHII_Pk_Ts1_Tb9_nf0.52_v2.npz")
else:
    print("Please choose a valid foreground model")

files.sort(reverse=True)

# List and sort all tfrecords files
bare_lc = glob.glob("../simulations/output/FullPara/*.tfrecord")
bare_lc.sort()

# Create mocks for the tfrecords file number N as specified by the user
dataset = tf.data.TFRecordDataset(bare_lc[int(args[0])])
dataset = dataset.map(parse_function,num_parallel_calls=tf.data.experimental.AUTOTUNE)
ds_numpy = tfds.as_numpy(dataset)

# For the given light-cone settings all light-cones have been created with 92 boxes at redshift values stored in redshifts5.npy
box_redshifts=np.load("../simulations/redshifts5.npy")
name=bare_lc[int(args[0])].split("/")
print("Creating mocks for"+str(bare_lc[int(args[0])]))
name="output/"+name[-1]
os.makedirs("output",exist_ok=True)
writer = tf.io.TFRecordWriter(name)

# For each light-cone in the specified tfrecords file
for ex in ds_numpy:
    delta_T=ex[0]
    label=ex[1]
        
    # 21cmFAST functions are used to derive the redshift associated with each pixel
    cosmo_params = p21c.CosmoParams(OMm=label[1])
    astro_params = p21c.AstroParams(INHOMO_RECO=True)
    user_params = p21c.UserParams(HII_DIM=140, BOX_LEN=200)
    flag_options = p21c.FlagOptions()
    simLightcone=p21c.LightCone(5.,user_params,cosmo_params,astro_params,flag_options,0,{"brightness_temp":delta_T},35.05)
    redshifts=simLightcone.lightcone_redshifts
    
    # Calculate the length of each box that was used to create the light-cone. Each box is associated with a single redshift and uses the calc_sense output file for the respective frequency
    box_len=np.array([])
    y=0
    z=0
    for x in range(len(delta_T[0][0])):
        if redshifts[x]>(box_redshifts[y+1]+box_redshifts[y])/2:
            box_len=np.append(box_len,x-z)
            y+=1
            z=x
    box_len=np.append(box_len,x-z+1)
  
    # Split the light-cone into the respective boxes
    y=0
    delta_T_split=False
    for x in box_len:
        if delta_T_split is False:
            delta_T_split=[delta_T[:,:,int(y):int(x+y)]]
        else:
            delta_T_split.append(delta_T[:,:,int(y):int(x+y)])
        y+=x

    output=False
    cell_size=200/140
    HII_DIM=140
    k140=np.fft.fftfreq(140,d=cell_size/2./np.pi)

    # For each box
    for x in range(len(box_len)):
        # Load 21cmSense output
        with np.load(files[x]) as data:
            ks = data["ks"]
            T_errs = data["T_errs"]

        # Calculate frequencies associated with the fourier transformation
        kbox=np.fft.rfftfreq(int(box_len[x]),d=cell_size/2./np.pi)
        VOLUME=HII_DIM*HII_DIM*box_len[x]*cell_size**3
        err21a=np.zeros((140,140,int(box_len[x])))
        err21b=np.zeros((140,140,int(box_len[x])))

        # Create a fourier transformation of the simulation
        deldel_T = np.fft.rfftn(delta_T_split[x],s=(HII_DIM,HII_DIM,box_len[x]))

        # Draw random real and imaginary phases
        err21a = np.random.normal(loc=0.0,scale=1.0,size=(HII_DIM,HII_DIM,int(box_len[x])))
        err21b = np.random.normal(loc=0.0,scale=1.0,size=(HII_DIM,HII_DIM,int(box_len[x])))
        deldel_T_noise=np.zeros((HII_DIM,HII_DIM,int(box_len[x])),dtype=np.complex_)
        deldel_T_mock=np.zeros((HII_DIM,HII_DIM,int(box_len[x])),dtype=np.complex_)
      
        # Read in noise and interpolate it to the respective k-values
        for n_x in range(HII_DIM):
            for n_y in range(HII_DIM):
                for n_z in range(int(box_len[x]/2+1)):
                    k_mag=math.sqrt(k140[n_x]**2+k140[n_y]**2+kbox[n_z]**2)
                    err21=np.interp(k_mag,ks,T_errs)

                    # Calculate and normalise the error in k space
                    if k_mag:
                        deldel_T_noise[n_x,n_y,n_z] = math.sqrt(math.pi*math.pi*VOLUME/k_mag**3*err21)*(err21a[n_x,n_y,n_z]+err21b[n_x,n_y,n_z]*1j)
                    else:
                        deldel_T_noise[n_x,n_y,n_z]=0
        
                    # Add noise to signal in k space. uv_box is a mask of 0 or 1 to make it possible to potentially ignore diverging values or areas with very high errors.
                    if(err21>=1000):
                        deldel_T_mock[n_x,n_y,n_z]=0
                    else:
                        deldel_T_mock[n_x,n_y,n_z]=deldel_T[n_x, n_y, n_z] + deldel_T_noise[n_x,n_y,n_z]/cell_size**3
            
        # Transform back into real space
        delta_T_mock=np.fft.irfftn(deldel_T_mock,s=(HII_DIM,HII_DIM,box_len[x]))

        #Rebuild the light-cone
        if output is False:
            output=delta_T_mock
        else:
            output=np.append(output,delta_T_mock,axis=2)
    
    save(output,label,writer)
writer.close()
