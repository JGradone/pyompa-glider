from __future__ import division, print_function
import cvxpy as cp
import numpy as np
import pandas as pd
import scipy
import scipy.spatial
import scipy.optimize
from collections import OrderedDict, defaultdict
import itertools


class OMPASoln(object):

    def __init__(self, endmember_df, endmember_name_column,
                       ompa_problem,
                       endmember_fractions,
                       converted_variables,
                       param_residuals,
                       groupname_to_effectiveconversionratios,
                       groupname_to_totalconvertedvariable,
                       nullspace_A,
                       **kwargs):
        if (endmember_df is not None):
            self.endmember_names=list(
                    endmember_df[self.endmember_name_column])
        self.endmember_df = endmember_df
        self.endmember_name_column = endmember_name_column
        self.ompa_problem = ompa_problem
        self.endmember_fractions = endmember_fractions
        self.converted_variables = converted_variables
        self.param_residuals = param_residuals
        self.groupname_to_effectiveconversionratios =\
              groupname_to_effectiveconversionratios
        self.groupname_to_totalconvertedvariable =\
              groupname_to_totalconvertedvariable
        if (ompa_problem is not None):
            self.obs_df = ompa_problem.obs_df
            self.param_names = self.ompa_problem.param_names
            self.endmembername_to_usagepenalty =\
                ompa_problem.endmembername_to_usagepenalty
        self.nullspace_A = nullspace_A
        self.__dict__.update(kwargs)

    def core_quantify_ambiguity_via_nullspace(self, obj_weights, verbose=False):
        #obj_weights should be an array of weights that define the objective
        # of the linear program, in the form "o @ (s + N(A) @ v)" (where
        # o is the objective, s is the solution, N(A) is the null space of A)

        #For each soln s (length "num_endmem + num_conv_ratios"), the
        # relevant linear programming constraints are
        #For rows pertaining to end member fractions:
        # "s + N(A) @ v >= 0" and "s + N(A) @ v <= 1" for endmember usgae
        # "s + N(A) @ v >= 0" OR "s + N(A) v <= 0" for oxygen usage 
        # (the 'or' means our solution space will basically contain two
        #  polytopes that may be disconnected (if there are multiple
        #  remin ratios; one polytope for positive and one for
        #  negative oxygen usage)
        #If there are any points with nonzero endmember usage constraints,
        # we can further add the constraint that P @ (N(A) @ v) = 0

        #Notes on how to determine the full feasible set (time complexity
        # seems to increase exponentially:)
        #If we are in |v| dimensional space, then the intersection of
        # |v| nonredundant equality constraints will define the corners. 
        #The available equality constraints are:
        # num_endmem constraints of the form endmem == 0
        # num_endmem constraints of the form endmem == 1
        # num_convratio constraints of the form endmem == 0
        # optionally one more constraint if there are nonzero usage penalties
        #Full solution:
        #From the (2*num_endmem + num_convratio + 1?) equations, we have
        # to check all (2*num_endmem + num_convratio + 1?)-choose-|v| subsets
        #Then for each subset, we need to solve the equation, and check
        # whether the solution satisfies the remaining constraints.
        #If the solution has negative O usage (has to be neg for all
        # conv ratios), we store the point in the negative O usage polytope;
        # otherwise, we store the point in the positive O usage polytope
        #We would then return the two polytopes
   
        #In practice, however, we are probably interested in quantifying the
        # limits of something (e.g. max and min values of an end member, or
        # max/min amounts of a tracer that could be explained by mixing),
        # in which case it is not necessary to solve for the full convex
        # polytope; the problem can just be solved as a linear program

        endmember_names = self.endmember_names
        endmember_usagepenalty =\
            self.ompa_problem.prep_endmember_usagepenalty_mat(endmember_names)

        #For each observation, we can solve the linear program
        new_perobs_endmember_fractions = []
        new_perobs_oxygen_deficits = []
        perobs_obj = []
        for obs_idx in range(len(self.endmember_fractions)):
            endmem_fracs = self.endmember_fractions[obs_idx] 
            assert len(endmem_fracs)==len(endmember_names)
            oxy_def = self.oxygen_deficits[obs_idx] 
            usagepenalty = endmember_usagepenalty[obs_idx]

            assert len(usagepenalty) == len(endmem_fracs)
            assert ((len(endmem_fracs) + len(oxy_def))
                    == self.nullspace_A.shape[0])
            assert len(obj_weights) == self.nullspace_A.shape[0]

            def compute_soln(oxy_def_sign):

                c = (obj_weights @ self.nullspace_A)

                A_ub = np.concatenate([
                        self.nullspace_A[:len(endmem_fracs)],
                        -(self.nullspace_A[:len(endmem_fracs)]),
                        -1*oxy_def_sign*self.nullspace_A[len(endmem_fracs):]
                    ], axis=0)
                b_ub = np.concatenate([ 1-endmem_fracs,
                                        endmem_fracs,
                                        np.zeros_like(oxy_def)], axis=0) 

                A_eq = (usagepenalty[None,:] @
                        self.nullspace_A[:len(endmem_fracs)])
                b_eq = 0

                res = scipy.optimize.linprog(c=c, A_ub=A_ub, b_ub=b_ub,
                                             A_eq=A_eq, b_eq=b_eq) 

                if (res.success == False):
                    fun = np.inf
                else:
                    fun = res.fun

                v = res.x

                endmem_frac_deltas = self.nullspace_A[:len(endmem_fracs)] @ v
                new_endmem_fracs = (endmem_fracs + endmem_frac_deltas)
                new_oxy_def = (oxy_def
                  + self.nullspace_A[len(endmem_fracs):] @ v)

                return ((new_endmem_fracs, new_oxy_def),
                        fun) #soln and optimal value

            if (self.nullspace_A.shape[1] > 0):
                posodsign_soln, posodsign_obj = compute_soln(1) 
                negodsign_soln, negodsign_obj = compute_soln(-1) 
                pos_wins = (posodsign_obj < negodsign_obj)
                if (pos_wins):
                    (new_endmem_fracs, new_oxy_def) = posodsign_soln
                else:
                    (new_endmem_fracs, new_oxy_def) = negodsign_soln

                assert new_endmem_fracs is not None
                assert np.abs(np.sum(new_endmem_fracs) - 1) < 1e-7
                #fix any numerical issues with soln
                new_endmem_fracs = np.maximum(new_endmem_fracs, 0)
                new_endmem_fracs = new_endmem_fracs/(np.sum(new_endmem_fracs))
                if (pos_wins):
                    new_oxy_def = np.maximum(new_oxy_def, 0)
                else:
                    new_oxy_def = np.minimum(new_oxy_def, 0)
            else:
                new_endmem_fracs = endmem_fracs
                new_oxy_def = oxy_def 

            obj = obj_weights@np.concatenate(
                   [new_endmem_fracs, new_oxy_def], axis=0) 

            new_perobs_endmember_fractions.append(new_endmem_fracs)
            new_perobs_oxygen_deficits.append(new_oxy_def)
            perobs_obj.append(obj) 

        return (np.array(new_perobs_endmember_fractions),
                np.array(new_perobs_oxygen_deficits),
                np.array(perobs_obj))

    def export_to_csv(self, csv_output_name,
                            orig_cols_to_include=[],
                            export_orig_param_vals=True,
                            export_residuals=True,
                            export_endmember_fracs=True,
                            export_converted_var_usage=True,
                            export_conversion_ratios=True,
                            export_endmember_usage_penalties=False):

        print("writing to",csv_output_name)

        toexport_df_dict = OrderedDict()

        if (export_orig_param_vals):
            orig_cols_to_include += self.param_names 

        for orig_col in orig_cols_to_include:
            assert orig_col in self.obs_df, (
             "Export settings asked that "+orig_col+" be copied from the"
             +" original observations data frame into the output, but"
             +" no such column header was present in the observations data"
             +" frame; the column headers are: "+str(self.obs_df.columns)) 
            toexport_df_dict[orig_col] = self.obs_df[orig_col]

        if (export_residuals):
             for param_idx,param_name in enumerate(self.param_names):
                toexport_df_dict[param_name+"_resid"] =\
                    self.param_residuals[:,param_idx] 

        endmember_names = self.endmember_names
        if (export_endmember_fracs):
            for endmember_idx in range(len(endmember_names)):
                toexport_df_dict[endmember_names[endmember_idx]+"_frac"] =\
                    self.endmember_fractions[:,endmember_idx]

        if (export_converted_var_usage):
            for groupname in self.groupname_to_totalconvertedvariable:
                toexport_df_dict[groupname] =\
                    self.groupname_to_totalconvertedvariable[groupname]

        if (export_conversion_ratios): 
            for groupname in self.groupname_to_effectiveconversionratios:
                effective_conversion_ratios =\
                    self.groupname_to_effectiveconversionratios[groupname]
                for converted_param in effective_conversion_ratios:
                    toexport_df_dict[converted_param+"_to_"
                     +groupname+"_ratio"] =\
                        effective_conversion_ratios[converted_param]

        if (export_endmember_usage_penalties):
            for endmembername in endmember_names:
                if (endmembername in\
                    self.ompa_problem.endmembername_to_usagepenalty): 
                    endmember_usagepenalty = (self.ompa_problem.
                                  endmembername_to_usagepenalty[endmembername])
                    toexport_df_dict[endmembername+"_penalty"] =\
                        endmember_usagepenalty
        
        toexport_df = pd.DataFrame(toexport_df_dict)
        toexport_df.to_csv(csv_output_name, index=False)

    def iteratively_refine_ompa_soln(self, num_iterations):
        init_endmember_df = self.ompa_problem.construct_ideal_endmembers(
            ompa_soln=self)
        ompa_solns = self.ompa_problem.iteratively_refine_ompa_solns(
            init_endmember_df=init_endmember_df,
            endmember_name_column=self.endmember_name_column,
            num_iterations=num_iterations)
        return [self]+ompa_solns


#Specifies a collection of parameters that are interconverted, and the
# convex hull of possible conversion ratios
class ConvertedParamGroup(object):

    def __init__(self, groupname, conversion_ratios, always_positive):
        #variable name is a string to indicate what the converted variable
        # represents (e.g. 'phosphate' or 'oxygen')
        #conversion_ratios should be a list of dictionaries from
        # parameter-->ratio relative to the remineralized variable 
        #always_positive is a boolean representing whether this variable
        # must always have a positive sign.
        assert len(conversion_ratios) >= 0
        #check that the keys are the same for all entries in conversionratios
        relevant_param_names = tuple(sorted(conversion_ratios[0].keys()))
        assert all([tuple(sorted(x.keys()))==relevant_param_names
                    for x in conversion_ratios]), ("The conversion_ratio"
                   +" dictionaries have inconsistent keys: "
                   +str(conversion_ratios))
        self.groupname = groupname
        self.conversion_ratios = conversion_ratios
        self.always_positive = always_positive
        self.relevant_param_names = relevant_param_names


class OMPAProblem(object):
    """
        Core class for conducting OMPA analysis using cvxpy
    """     
    def __init__(self, obs_df,
                       param_names, #both conserved and converted params
                       convertedparam_groups,
                       param_weightings,
                       endmembername_to_usagepenaltyfunc={},
                       smoothness_lambda=None,
                       sumtooneconstraint=True,
                       standardize_by_watertypes=False):
        self.obs_df = obs_df
        self.param_names = param_names
        self.convertedparam_groups = convertedparam_groups
        self.standardize_by_watertypes = standardize_by_watertypes

        self.num_converted_variables = (
            0 if len(convertedparam_groups)==0 else sum([
                len(x.conversion_ratios) for x in convertedparam_groups]))
        assert (len(set([x.groupname for x in convertedparam_groups]))
                ==len(convertedparam_groups)), (
                "Duplicate groupnames in convertedparam_groups: "
                +str([x.groupname for x in convertedparam_groups]))

        self.param_weightings = param_weightings

        self.smoothness_lambda = smoothness_lambda
        self.endmembername_to_usagepenaltyfunc =\
          endmembername_to_usagepenaltyfunc
        self.process_params()
        self.prep_endmember_usagepenalties() 
        self.sumtooneconstraint = sumtooneconstraint #apply hard contraint

    def process_params(self):

        #check that a parameter weighting is specified for every parameter
        # and that the parameter appears in the obs df
        for param_name in self.param_names:
            assert param_name in self.param_weightings, (
                param_name+" not in parameter weightings: "
                +str(self.param_weightings)) 
            assert param_name in self.obs_df,\
                (param_name+" not specified in observations data frame;"
                 +" observations data frame columns are "
                 +str(list(self.obs_df.columns)))

        with_drop_na = self.obs_df.dropna(subset=self.param_names)
        if (len(with_drop_na) < len(self.obs_df)):
            print("Dropping "+str(len(self.obs_df)-len(with_drop_na))
                  +" rows that have NA values in the observations")
            self.obs_df = with_drop_na

        if (max(self.param_weightings.values()) > 100):
            print("Warning: having very large param weights can lead to"
                  +" instability in the optimizer! Consider scaling down"
                  +" your weights")

    def prep_endmember_usagepenalties(self):
        self.endmembername_to_usagepenalty = OrderedDict()
        for endmembername, penalty_func in\
         self.endmembername_to_usagepenaltyfunc.items():
            print("Adding penalty for",endmembername)
            penalty = penalty_func(self.obs_df)
            self.endmembername_to_usagepenalty[endmembername] = penalty

    def prep_endmember_usagepenalty_mat(self, endmember_names):
        endmember_usagepenalty = np.zeros((len(self.obs_df),
                                           len(endmember_names)))
        for endmemberidx,endmembername in enumerate(endmember_names):
            if endmembername in self.endmembername_to_usagepenalty:
                endmember_usagepenalty[:,endmemberidx] =\
                    self.endmembername_to_usagepenalty[endmembername]
        #print a warning if specified a usage penalty that was not used
        for endmembername in self.endmembername_to_usagepenalty:
            if endmembername not in endmember_names:
                raise RuntimeError("You specified a usage penalty for "
                 +endmembername+" but that endmember did not appear "
                 +"in the endmember data frame; endmembers are "
                 +str(endmember_names))
                 
        return endmember_usagepenalty

    def get_b(self):
        b = np.array(self.obs_df[self.param_names])
        return b

    def get_param_weighting(self):
        return np.array([self.param_weightings[x] for x in self.param_names])

    def get_conversion_ratio_rows_of_A(self):
        rows = []
        groupname_to_conversionratiosdict = OrderedDict()
        for convertedparam_group in self.convertedparam_groups:
            conversion_ratios_dict = defaultdict(list)
            for conversion_ratio in convertedparam_group.conversion_ratios: 
                rows.append([conversion_ratio.get(param_name,0)
                             for param_name in self.param_names])
                [conversion_ratios_dict[relevant_param_name].append(
                        conversion_ratio[relevant_param_name])
                 for relevant_param_name
                 in convertedparam_group.relevant_param_names]
            groupname_to_conversionratiosdict[
              convertedparam_group.groupname] = conversion_ratios_dict
        return groupname_to_conversionratiosdict, np.array(rows)

    def get_convertedvariable_signcombos_to_try(self):
        #Get the different sign constraints to apply to the converted variables
        #carprod = cartesian product
        # '0' indicates no sign constraint necessary because there's just
        # one conversionratio in the group
        to_take_cartprod_of = []
        for convertedparam_group in self.convertedparam_groups:
            allpos = tuple([1 for x in
                            convertedparam_group.conversion_ratios])
            allneg = tuple([-1 for x in
                            convertedparam_group.conversion_ratios])
            #indifferent = tuple([0 for x in
            #                     convertedparam_group.conversion_ratios])
            if (convertedparam_group.always_positive):
                to_take_cartprod_of.append((allpos,))
            else:
                #if (len(convertedparam_group.conversion_ratios)==1):
                #    to_take_cartprod_of.append((indifferent,))
                #else:
                to_take_cartprod_of.append((allpos,allneg))
            
        cartprod = list(itertools.product(*to_take_cartprod_of))
        return [np.array(list(itertools.chain(*x))) for x in cartprod] 

    def get_endmem_mat(self, endmember_df):
        return np.array(endmember_df[self.param_names])

    def get_nullspace(self, M, R):
        #Let M represent the end-member matrix - dimensions of end_mem x params
        # the R represent the conversion ratios -
        #  dims of num_conv_ratio_rows x params
        # |M.T R.T| x = 0
        # |1   0  | 
        # (where |1 0| represents the mass conservation constraint)
        #The solutions of x will give us the null space
        mat = np.concatenate(
               [(np.concatenate([np.transpose(M), np.transpose(R)], axis=1)
                 if len(R) > 0 else np.transpose(M)),
                np.array([1 for i in range(len(M))]
                         +[0 for i in range(len(R))])[None,:]
               ], axis=0)
        ns = scipy.linalg.null_space(mat) 
        if ns.shape[1] > 0:
            assert np.allclose(mat.dot(ns), 0)
        return ns

    def solve(self, endmember_df, endmember_name_column):

        for param_name in self.param_names:
            assert param_name in endmember_df,\
                (param_name+" not specified in endmember_df where columns are "
                 +str(list(endmember_df.columns)))

        endmember_names = list(endmember_df[endmember_name_column])

        weighting = self.get_param_weighting() 
        smoothness_lambda = self.smoothness_lambda

        endmember_usagepenalty =\
            self.prep_endmember_usagepenalty_mat(endmember_names)
        self.endmember_usagepenalty = endmember_usagepenalty

        #Prepare A
        groupname_to_conversionratiosdict, conversion_ratio_rows =\
                self.get_conversion_ratio_rows_of_A()
        #conversion_ratio_rows = 'R'
        endmem_mat = self.get_endmem_mat(endmember_df) #'M'
        if (len(conversion_ratio_rows > 0)):
            #add rows to A for the conversion ratios
            A = np.concatenate([endmem_mat, conversion_ratio_rows], axis=0)
        else:
            A = endmem_mat

        if (self.standardize_by_watertypes):
            param_mean = np.mean(endmem_mat, axis=0) 
            #if std is inf, set to 1
            param_std = np.std(endmem_mat, axis=0)
            mass_idxs = np.nonzero(1.0*(param_std==0))[0]
            param_std[mass_idxs] = 1.0
            #Assume that the entry with std of 0 is also the one that has
            # mass in it
            print("I'm assuming that the index encoding mass is:",
                  mass_idxs)
            if (len(mass_idxs) > 1):
                raise RuntimeError("Multiple indices in source water type"
                +" matrix have 0 std...I need to be told which one"
                +" is 'mass' "+str(endmem_mat))
            param_mean[mass_idxs] = 0.0
            print("Std used for normalization:",param_std)
            print("Mean used for normalization:",param_mean)

        #compute the nullspace of A - will be useful for disentangling
        # ambiguity in the solution
        nullspace_A = self.get_nullspace(M=endmem_mat,
                                         R=conversion_ratio_rows)

        #prepare b
        b = self.get_b()
        
        #Rescale by param weighting
        print("params to use:", self.param_names)        
        print("param weighting:", weighting)
        if (self.standardize_by_watertypes):
            weighting = weighting/param_std
            print("effective weighting:", weighting/param_std)
        print("Conversion ratios:\n"
              +str(groupname_to_conversionratiosdict))
        orig_A = A
        orig_b = b
        if (self.standardize_by_watertypes):
            A = A-param_mean[None,:]
            b = b-param_mean[None,:]
        A = A*weighting[None,:]
        b = b*weighting[None,:]

        if (smoothness_lambda is not None):
            pairs_matrix = make_pairs_matrix(
              obs_df=self.obs_df,
              depth_metric="depth",
              depth_scale=1.0,
              nneighb=4)
        else:
            pairs_matrix = None

        if (self.num_converted_variables > 0):
            signcombos_to_try = self.get_convertedvariable_signcombos_to_try()
            perobs_weighted_resid_sq_for_signcombo = []
            for signcombo in signcombos_to_try:
                print("Trying convertedvariable sign constraint:",signcombo)
                _, _, _, perobs_weighted_resid_sq_positiveconversionsign, _ =\
                  self.core_solve(
                    A=A, b=b,
                    num_converted_variables=self.num_converted_variables,
                    pairs_matrix=None,
                    endmember_usagepenalty=endmember_usagepenalty,
                    conversion_sign_constraints=signcombo[None,:],
                    smoothness_lambda=None)
                perobs_weighted_resid_sq_for_signcombo.append(
                    perobs_weighted_resid_sq_positiveconversionsign)
            #determine which conversion sign is best for each example
            perobs_weighted_resid_sq_for_signcombo =\
                np.array(perobs_weighted_resid_sq_for_signcombo) 
            best_sign_combos = np.array([signcombos_to_try[bestidx]
               for bestidx in np.argmin(perobs_weighted_resid_sq_for_signcombo,
                                        axis=0)])
        else:
            best_sign_combos = None
        
        (x, endmember_fractions,
         converted_variables,
         perobs_weighted_resid_sq, prob) = self.core_solve(
            A=A, b=b,
            num_converted_variables=self.num_converted_variables,
            pairs_matrix=pairs_matrix,
            endmember_usagepenalty=endmember_usagepenalty,
            conversion_sign_constraints=best_sign_combos,
            smoothness_lambda=smoothness_lambda)
        
        if (endmember_fractions is not None):
            print("objective:", np.sum(perobs_weighted_resid_sq))
            #get the reconstructed parameters and residuals in the original
            # parameter space
            param_reconstruction = x@orig_A
            param_residuals =  param_reconstruction - orig_b
        else:
            param_residuals = None

        groupname_to_convertedvariables = {}
        groupname_to_totalconvertedvariable = OrderedDict()
        groupname_to_effectiveconversionratios = OrderedDict()
        convar_idx = 0
        for convertedparam_group in self.convertedparam_groups:
            groupname = convertedparam_group.groupname
            convar_vals = converted_variables[:,
                convar_idx:
                convar_idx+len(convertedparam_group.conversion_ratios)]
            convar_idx += len(convertedparam_group.conversion_ratios)
            #sanity check the signs; for each entry they
            # should either be all positive or all negative, within numerical
            # precision
            for convar_vals_row in convar_vals:
                if ((all(convar_vals_row >= 0) or
                     all(convar_vals_row <= 0))==False):
                    print("WARNING: sign inconsistency in "
                          +groupname+":", convar_vals_row)
            total_convar = np.sum(convar_vals, axis=-1)
            groupname_to_totalconvertedvariable[groupname] =\
                total_convar 
            #proportions of oxygen use at differnet ratios
            with np.errstate(divide='ignore', invalid='ignore'):
                convarusage_proportions = (
                    convar_vals/total_convar[:,None])
                effective_conversion_ratios = OrderedDict()
                conversion_ratios_dict = (
                  groupname_to_conversionratiosdict[groupname])
                #Reminder: conversion_ratios has dims of
                # num_converted_variables x num_converted_params
                #oxygen_usage_proportions has dims of
                # num_examples X num_converted_variables
                #Note: should disregard conversion ratios computed when oxygen
                # usage is very small.
                effective_conversion_ratios = OrderedDict([
                    (relevant_param_name,
                     convarusage_proportions@conversion_ratio)
                    for relevant_param_name,conversion_ratio in
                    conversion_ratios_dict.items()
                 ]) 
                groupname_to_effectiveconversionratios[groupname] =\
                    effective_conversion_ratios
        else:
            total_oxygen_deficit = None
            effective_conversion_ratios = None

        return OMPASoln(endmember_df=endmember_df, ompa_problem=self,
                  endmember_names=endmember_names,
                  endmember_name_column=endmember_name_column,
                  status=prob.status,
                  endmember_fractions=endmember_fractions,
                  converted_variables=converted_variables,
                  resid_wsumsq=np.sum(perobs_weighted_resid_sq),
                  param_residuals=param_residuals,
                  groupname_to_totalconvertedvariable=
                    groupname_to_totalconvertedvariable,
                  groupname_to_effectiveconversionratios=
                    groupname_to_effectiveconversionratios,
                  nullspace_A=nullspace_A)

    def core_solve(self, A, b, num_converted_variables,
                   pairs_matrix, endmember_usagepenalty,
                   conversion_sign_constraints, smoothness_lambda,
                   verbose=True):
  
        #We are going to solve the following problem:
        #P is the penalty matrix. It has dimensions of
        #  (observations X end_members)
        #Minimize (x@A - b)^2 + (x[:,:-num_converted_variables]*P)^2
        #Subject to x[:,:-num_converted_variables] >= 0,
        #           cp.sum(x[:,:-num_converted_variables], axis=1) == 1
        # x has dimensions of observations X (end_members+num_converted_variables)
        # the +1 row represents O2 deficit, for remineralization purposes
        # A has dimensions of (end_members+num_converted_variables) X parameteres
        # b has dimensions of observations X parameters 
        
        num_endmembers = len(A)-num_converted_variables
        x = cp.Variable(shape=(len(b), len(A)))
        obj = (cp.sum_squares(x@A - b) +
                cp.sum_squares(cp.atoms.affine.binary_operators.multiply(
                                    x[:,:num_endmembers],
                                    endmember_usagepenalty) ))
        if (smoothness_lambda is not None):
            #leave out O2 deficit column from the smoothness penality as it's
            # on a bit of a different scale.
            obj += smoothness_lambda*cp.sum_squares(
                    pairs_matrix@x[:,:num_endmembers])
        obj = cp.Minimize(obj)
        
        #leave out the last column as it's the conversion ratio
        constraints = [x[:,:num_endmembers] >= 0]
        if (self.sumtooneconstraint):
            constraints.append(cp.sum(x[:,:num_endmembers],axis=1)==1)

        if (len(self.convertedparam_groups) > 0):
            constraints.append(
              cp.atoms.affine.binary_operators.multiply(
                  (conversion_sign_constraints if
                   len(conversion_sign_constraints)==len(b) else
                   np.tile(conversion_sign_constraints, (len(b),1))),
                  x[:,num_endmembers:]) >= 0)

        prob = cp.Problem(obj, constraints)
        prob.solve(verbose=False, max_iter=50000)
        #settign verbose=True will generate more print statements and
        # slow down the analysis
        
        print("status:", prob.status)
        print("optimal value", prob.value)

        if (prob.status=="infeasible"):
            raise RuntimeError("Optimization failed - "
                               +"try lowering the parameter weights?")
        else:
            #weighted sum of squared of the residuals
            original_resid_wsumsq = np.sum(np.square((x.value@A) - b))

            endmember_fractions = x.value[:,:num_endmembers]
            ##enforce the constraints (nonnegativity, sum to 1) on
            ## endmember_fractions
            endmember_fractions = np.maximum(endmember_fractions, 0) 
            if (self.sumtooneconstraint):
                endmember_fractions = (endmember_fractions/
                    np.sum(endmember_fractions,axis=-1)[:,None])

            if (len(self.convertedparam_groups) > 0):
                converted_variables = conversion_sign_constraints*np.maximum(
                                         (conversion_sign_constraints
                                          *x.value[:, num_endmembers:]), 0.0)
                #fixed_x is x that is forced to satisfy the constraints
                fixed_x = np.concatenate(
                           [endmember_fractions, converted_variables], axis=-1)
            else:
                converted_variables = None
                fixed_x = endmember_fractions

            afterfixing_resid_wsumsq = np.sum(np.square(fixed_x@A - b))
            print("Original weighted sum squares:",original_resid_wsumsq)
            print("Post fix weighted sum squared:",afterfixing_resid_wsumsq)

            perobs_weighted_resid_sq =\
                np.sum(np.square((fixed_x@A) - b), axis=-1)
        
        return (fixed_x, endmember_fractions, converted_variables,
                perobs_weighted_resid_sq, prob)

    def construct_ideal_endmembers(self, ompa_soln):

        print("Constructing ideal end members")

        b = self.get_b() # dims of num_obs X params

        if (ompa_soln.oxygen_deficits is not None):
            #self.oxygen_deficits has dims of num_obs X num_converted_variables
            #conversion_ratio_rows has dims of num_converted_variables X params
            _, conversion_ratio_rows = self.get_conversion_ratio_rows_of_A()
            deltas_due_to_oxygen_deficits = ( #<- num_obs X params
             ompa_soln.oxygen_deficits@conversion_ratio_rows) 
            b = b - deltas_due_to_oxygen_deficits

        #Do a sanity check to make sure that, with the existing end member
        # matrix, we end up recapitulating the residuals
        #existing_endmemmat has dims of num_endmembers X params
        existing_endmemmat = self.get_endmem_mat(ompa_soln.endmember_df)
        #self.endmember_fractions has dims of num_obs X num_endmembers
        #Note: b and existing_endmemmat haven't been scaled by weighting yet,
        # so there is no need
        # to un-scale it here. Also note deltas_due_to_oxygen_deficits has
        # already been subtracted
        old_param_residuals = (b
          - ompa_soln.endmember_fractions@existing_endmemmat)
        np.testing.assert_almost_equal(old_param_residuals,
                                       ompa_soln.param_residuals, decimal=5)

        #Now solve for a better end-member mat
        weighting = self.get_param_weighting()
        new_endmemmat_var =  cp.Variable(shape=existing_endmemmat.shape)  
        
        #keeping the endmember_fractions, what are the best end members?
        obj = cp.Minimize(
            cp.sum_squares(ompa_soln.endmember_fractions@new_endmemmat_var
                           - b*weighting[None,:]))
        constraints = [] #no constraints for now
        prob = cp.Problem(obj, constraints)
        prob.solve(verbose=False, max_iter=50000)
        
        print("status:", prob.status)
        print("optimal value", prob.value)

        if (prob.status=="infeasible"):
            raise RuntimeError("Something went wrong "
                               +"- the optimization failed")
        else:
            new_endmemmat = new_endmemmat_var.value/weighting[None,:]
            #Sanity check that the residuals got better
            new_param_residuals = (b
                - ompa_soln.endmember_fractions@new_endmemmat)
            new_param_resid_wsumsq =\
                np.sum(np.square(new_param_residuals*weighting[None,:]))
            old_param_resid_wsumsq =\
                np.sum(np.square(old_param_residuals*weighting[None,:]))
            print("Old weighted residuals sumsquared:",old_param_resid_wsumsq)
            print("New weighted residuals sumsquared:",new_param_resid_wsumsq)
            assert new_param_resid_wsumsq <= old_param_resid_wsumsq

            #make a endmember data frame
            new_endmemmat_df = pd.DataFrame(OrderedDict(
             [(ompa_soln.endmember_name_column,
               list(ompa_soln.endmember_df[ompa_soln.endmember_name_column]))]
             +[(paramname, values) for paramname,values in
             zip(self.conserved_params_to_use+self.converted_params_to_use,
                 new_endmemmat.T) ])) 
            return new_endmemmat_df

    def iteratively_refine_ompa_solns(self, init_endmember_df,
            endmember_name_column, num_iterations):
        assert num_iterations >= 1,\
            "num_iterations must be >= 1; is "+str(num_iterations)
        print("On iteration 1")
        ompa_solns = [self.solve(endmember_df=init_endmember_df,
                                 endmember_name_column=endmember_name_column)]
        for i in range(1,num_iterations):
            print("On iteration "+str(i+1))
            new_endmember_df =\
                self.construct_ideal_endmembers(ompa_soln=ompa_solns[-1]) 
            ompa_solns.append(self.solve(endmember_df=new_endmember_df,
                                  endmember_name_column=endmember_name_column))
        return ompa_solns 


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
    
