from __future__ import division, print_function
import cvxpy as cp
import numpy as np
import pandas as pd
import scipy
import scipy.spatial


class OMPAProblem(object):
    """
        Core class for conducting OMPA analysis using cvxpy
    """     
    def __init__(self, watermass_df,
                       obs_df,
                       paramsandweighting_conserved,
                       paramsandweighting_converted,
                       conversionratios,
                       smoothness_lambda,
                       watermassname_to_usagepenaltyfunc):
        self.watermass_df = watermass_df
        self.watermassnames = list(watermass_df["watermassname"])
        self.obs_df = obs_df
        self.paramsandweighting_conserved = paramsandweighting_conserved
        self.paramsandweighting_converted = paramsandweighting_converted
        self.conversionratios = conversionratios
        self.smoothness_lambda = smoothness_lambda
        self.watermassname_to_usagepenaltyfunc =\
          watermassname_to_usagepenaltyfunc
        self.process_params()

    def prep_watermass_usagepenalty_mat(self):
        obs_df = self.obs_df
        watermassnames = self.watermassnames
        watermass_usagepenalty = np.zeros((len(obs_df),
                                           len(watermassnames)))
        for watermassidx,watermassname in enumerate(watermassnames):
            if watermassname in self.watermassname_to_usagepenaltyfunc:
                lat = np.array(obs_df["latitude"])
                sig0 = np.array(obs_df["sig0"])
                penalty = self.watermassname_to_usagepenaltyfunc[watermassname](
                    lat=lat, sig0=sig0)
                watermass_usagepenalty[:,watermassidx] = penalty
                #Plotting
                from matplotlib import pyplot as plt
                print("Adding penalty for",watermassname)
                plt.scatter(lat, -np.array(obs_df["depth"]), c=penalty)
                plt.colorbar()
                plt.show()
        return watermass_usagepenalty

    def process_params(self):
        #check that every param in self.paramsandweighting_converted is
        # specified in convertedparams_ratios
        
        #paramsandweighting_conserved is a list of tuples; split them up
        print(self.paramsandweighting_converted)
        self.conserved_params_to_use, self.conserved_weighting = [
          list(x) for x in zip(*self.paramsandweighting_conserved)]
        if (len(self.paramsandweighting_converted) > 0):
            self.converted_params_to_use, self.converted_weighting = [
              list(x) for x in zip(*self.paramsandweighting_converted)]
        else:
            self.converted_params_to_use, self.converted_weighting = [], []
        
        #make sure every parameter in converted_params_to_use is specified in
        # convertedparams_ratios:
        assert all([(x in self.conversionratios)
                     for x in self.converted_params_to_use])
        #also assert that every entry in convertedratios has the same length
        assert len(set([len(x) for x in self.conversionratios.values()])) == 1
        self.num_conversion_ratios = len(
            list(self.conversionratios.values())[0])

    def solve(self):

        watermass_df = self.watermass_df        
        obs_df = pd.DataFrame(self.obs_df)
        conserved_params_to_use = self.conserved_params_to_use
        converted_params_to_use = self.converted_params_to_use
        weighting = np.array(self.conserved_weighting+self.converted_weighting)
        #conversion_ratios has dimensions:
        # num_conversion_ratios x num_converted_params
        conversion_ratios = np.array([[self.conversionratios[param][i]
                                       for param in converted_params_to_use]
                                    for i in range(self.num_conversion_ratios)])

        watermass_usagepenalty = self.prep_watermass_usagepenalty_mat()
        self.watermass_usagepenalty = watermass_usagepenalty

        print("Conversion ratios:\n"+str(conversion_ratios))
        A = np.array(watermass_df[conserved_params_to_use
                                  +converted_params_to_use])
        #add a row to A for the ratios
        A = np.concatenate([A]+[
                np.array([0 for x in conserved_params_to_use]
                          +list(converted_ratio_row))[None,:]
                for converted_ratio_row in conversion_ratios],
              axis=0)
        b = np.array(obs_df[conserved_params_to_use+converted_params_to_use])
        
        print("params to use:", conserved_params_to_use,
                                converted_params_to_use)        
        print("param weighting:", weighting)
        print("ratio", conversion_ratios)
        A = A*weighting[None,:]
        b = b*weighting[None,:]

        if (self.smoothness_lambda is not None):
            pairs_matrix = make_pairs_matrix(
              obs_df=obs_df,
              depth_metric="depth",
              depth_scale=1.0,
              nneighb=4)
        else:
            pairs_matrix = None

        #first run with only a positive conversion ratio allowed
        _, _, _, individual_residuals_positiveconversionsign, _ =\
          self.core_solve(
            A=A, b=b, conversion_ratios=conversion_ratios, pairs_matrix=None,
            watermass_usagepenalty=watermass_usagepenalty,
            conversion_sign_constraints=1,
            smoothness_lambda=None)
        _, _, _, individual_residuals_negativeconversionsign, _ =\
          self.core_solve(
            A=A, b=b, conversion_ratios=conversion_ratios, pairs_matrix=None,
            watermass_usagepenalty=watermass_usagepenalty,
            conversion_sign_constraints=-1,
            smoothness_lambda=None)
        
        #determine which conversion sign is better
        positive_conversionsign_isbetter = (
            individual_residuals_positiveconversionsign
            < individual_residuals_negativeconversionsign)
        final_conversion_signconstraints = (
            1.0*positive_conversionsign_isbetter
            + -1.0*(positive_conversionsign_isbetter==False))
        
        (x, water_mass_fractions,
         oxygen_deficits,
         residuals_squared, prob) = self.core_solve(
            A=A, b=b, conversion_ratios=conversion_ratios,
            pairs_matrix=pairs_matrix,
            watermass_usagepenalty=watermass_usagepenalty,
            conversion_sign_constraints=final_conversion_signconstraints,
            smoothness_lambda=smoothness_lambda)
        
        self.prob = prob   

        if (water_mass_fractions is not None):
            print("objective:", np.sum(residuals_squared))
            param_reconstruction = (x@A)/weighting[None,:]
            param_residuals = b/weighting[None,:] - param_reconstruction
            self.water_mass_fractions = water_mass_fractions
            self.param_reconstruction = param_reconstruction
            self.param_residuals = param_residuals

        if (oxygen_deficits is not None):
            #sanity check the signs of the oxygen deficits; for each entry they
            # should either be all positive or all negative
            for oxygen_deficit in oxygen_deficits:
                if (len(oxygen_deficit) > 0):
                    assert all(oxygen_deficit > -1e-6)\
                          or all(oxygen_deficit < 1e-6), oxygen_deficit
            total_oxygen_deficit = np.sum(oxygen_deficits, axis=-1)
            #proportions of oxygen use at differnet ratios
            oxygen_usage_proportions = (oxygen_deficits/
                                        total_oxygen_deficit[:,None])
            #Reminder: conversion_ratios has dims of
            # num_conversion_ratios x num_converted_params
            #oxygen_usage_proportions has dims of
            # num_examples X num_conversion_ratios
            effective_conversion_ratios = (
                oxygen_usage_proportions@conversion_ratios)         
            self.total_oxygen_deficit = total_oxygen_deficit
            self.effective_conversion_ratios = effective_conversion_ratios
        else:
            self.total_oxygen_deficit = None
            self.effective_conversion_ratios = None

    def core_solve(self, A, b, conversion_ratios,
                   pairs_matrix, watermass_usagepenalty,
                   conversion_sign_constraints, smoothness_lambda,
                   verbose=True):
  
        #We are going to solve the following problem:
        #P is the penalty matrix. It has dimensions of
        #  (observations X end_members)
        #Minimize (x@A - b)^2 + (x[:,:-len(conversion_ratios)]*P)^2
        #Subject to x[:,:-len(conversion_ratios)] >= 0,
        #           cp.sum(x[:,:-len(conversion_ratios)], axis=1) == 1
        # x has dimensions of observations X (end_members+len(conversion_ratios))
        # the +1 row represents O2 deficit, for remineralization purposes
        # A has dimensions of (end_members+len(conversion_ratios)) X parameteres
        # b has dimensions of observations X parameters 
        
        num_watermasses = len(A)-len(conversion_ratios)
        x = cp.Variable(shape=(len(b), len(A)))
        obj = (cp.sum_squares(x@A - b) +
                cp.sum_squares(cp.atoms.affine.binary_operators.multiply(
                                    x[:,:num_watermasses],
                                    watermass_usagepenalty) ))
        if (smoothness_lambda is not None):
            #leave out O2 deficit column from the smoothness penality as it's
            # on a bit of a different scale.
            obj += smoothness_lambda*cp.sum_squares(
                    pairs_matrix@x[:,:num_watermasses])
        obj = cp.Minimize(obj)
        
        #leave out the last column as it's the conversion ratio
        constraints = [
           x[:,:num_watermasses] >= 0,
           cp.sum(x[:,:num_watermasses],axis=1)==1]
        if (len(conversion_ratios) > 0):
            if (hasattr(conversion_sign_constraints, '__len__')==False):
                constraints.append(
                  cp.atoms.affine.binary_operators.multiply(
                      conversion_sign_constraints,
                      x[:,num_watermasses:]) >= 0)
            else:
                constraints.append(
                  cp.atoms.affine.binary_operators.multiply(
                      np.tile(A=conversion_sign_constraints[:,None],
                              reps=(1,len(conversion_ratios))),
                      x[:,num_watermasses:]) >= 0)
        prob = cp.Problem(obj, constraints)
        prob.solve(verbose=False, max_iter=50000)
        #settign verbose=True will generate more print statements and slow down the analysis
        
        print("status:", prob.status)
        print("optimal value", prob.value)

        if (prob.status=="infeasible"):
            water_mass_fractions = None
            oxygen_deficits = None
            residuals_squared = None
        else:
          water_mass_fractions = x.value[:,:num_watermasses]
          if (conversion_ratios.shape[1] > 0):
             oxygen_deficits = x.value[:,num_watermasses:]
          else:
             oxygen_deficits = None
          residuals_squared = np.sum(np.square((x.value@A) - b), axis=-1)
        
        return (x.value,
                water_mass_fractions,
                oxygen_deficits,
                residuals_squared, prob)


def spherical_to_surface_cartesian(lat, lon):
    r = 6.371*(1E3) #earth radius
    theta = ((1-lat)/180.0)*np.pi
    phi = (lon/180.0)*np.pi
    x = r*np.sin(theta)*np.cos(phi)
    y = r*np.sin(theta)*np.sin(phi)
    return (x,y)


def add_surface_cartesian_coordinates_to_df(df):
    latitudes = df["latitude"]
    longitudes = df["longitude"]
    xs,ys = list(zip(*[spherical_to_surface_cartesian(*x)
                       for x in zip(latitudes, longitudes)]))
    df["x"] = xs
    df["y"] = ys
    #plt.scatter(xs, ys)
    #plt.show()


def compute_pairwise_distances_depthmetric(df, depth_metric, depth_scale):
    xs = df["x"]
    ys = df["y"]
    
    depth_metric = np.array(df[depth_metric])
    depth_diffs = np.abs(depth_metric[:,None] -
                         depth_metric[None,:])*depth_scale

    #plt.hist(depth_diffs.ravel(), bins=20)
    #plt.show()

    coors = np.array([xs, ys]).transpose((1,0))
    euclidean_distances = scipy.spatial.distance.squareform(
        scipy.spatial.distance.pdist(coors))
    #plt.hist(euclidean_distances.ravel(), bins=100)
    #plt.show()

    weighted_distances = np.sqrt(np.square(euclidean_distances)
                                 + np.square(depth_diffs))
    #plt.hist(weighted_distances.ravel(), bins=20)
    #plt.show()
    return weighted_distances


def make_pairs_matrix(obs_df, depth_metric, depth_scale, nneighb):
    obs_df = pd.DataFrame(obs_df)
    add_surface_cartesian_coordinates_to_df(obs_df)
    pairwise_distances = compute_pairwise_distances_depthmetric(
        obs_df, depth_metric=depth_metric, depth_scale=depth_scale)
    #plt.hist(pairwise_distances.ravel(), bins=20)
    #plt.show()
    nneighb_thresh = np.sort(pairwise_distances, axis=-1)[:,nneighb]
    masked_pairwise_distances =\
      (pairwise_distances*(pairwise_distances <= nneighb_thresh[:,None])
                         *(pairwise_distances > 0))
    pairs_to_consider_indices = np.nonzero(masked_pairwise_distances)
    print("Constrained pairs:",len(pairs_to_consider_indices[0]))
    pairs_distances = pairwise_distances[
        pairs_to_consider_indices[0],
        pairs_to_consider_indices[1]]
    #plt.hist(pairs_distances.ravel(), bins=20)
    #plt.show()
    pairs_matrix = np.zeros((len(pairs_to_consider_indices[0]),
                              len(obs_df)))
    pairs_matrix[np.arange(len(pairs_distances)),
                  pairs_to_consider_indices[0]] = 1.0/nneighb#(
                      #1/pairs_distances)
    pairs_matrix[np.arange(len(pairs_distances)),
                  pairs_to_consider_indices[1]] = -1.0/nneighb#(
                      #1/pairs_distances)
    return pairs_matrix
