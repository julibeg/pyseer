# Copyright 2017 Marco Galardini and John Lees

'''Python reimplementation of SEER for bacterial GWAS'''

import os
import sys
import gzip
import warnings
import itertools
import operator
import re
from collections import deque
from .utils import set_env
# avoid numpy taking up more than one thread
with set_env(MKL_NUM_THREADS='1',
             NUMEXPR_NUM_THREADS='1',
             OMP_NUM_THREADS='1'):
    import numpy as np
import pandas as pd
from sklearn import manifold
from multiprocessing import Pool
from pysam import VariantFile

from .__init__ import __version__

from .input import iter_variants
from .input import load_structure
from .input import load_lineage
from .input import load_phenotypes
from .input import load_covariates
from .input import load_burden

from .model import binary
from .model import fit_null
from .model import continuous

from .utils import format_output


def get_options():
    import argparse

    description = 'SEER (doi: 10.1038/ncomms12797), reimplemented in python'
    parser = argparse.ArgumentParser(description=description,
                                     prog='pyseer')

    phenotypes = parser.add_argument_group('Phenotype')
    phenotypes.add_argument('--phenotypes',
                            required=True,
                            help='Phenotypes file')
    phenotypes.add_argument('--phenotype-col',
                            type=int,
                            default=0,
                            help='Phenotype file column to use '
                                 '[Default: 0, meaning last column]')

    variants = parser.add_argument_group('Variants')
    variant_group = variants.add_mutually_exclusive_group(required=True)
    variant_group.add_argument('--kmers',
                               default=None,
                               help='Kmers file')
    variant_group.add_argument('--vcf',
                               default=None,
                               help='VCF file. Will filter any non '
                                    '\'PASS\' sites')
    variant_group.add_argument('--pres',
                               default=None,
                               help='Presence/absence .Rtab matrix as '
                                    'produced by roary and piggy')

    distances = parser.add_argument_group('Distances')
    distance_group = distances.add_mutually_exclusive_group(required=True)
    distance_group.add_argument('--distances',
                                help='Strains distance square matrix')
    distance_group.add_argument('--load-m',
                                help='Load an existing matrix decomposition')
    distances.add_argument('--save-m',
                           help='Prefix for saving matrix decomposition')
    distances.add_argument('--mds',
                           default="classic",
                           choices=['classic', 'metric', 'non-metric'],
                           help='Type of multidimensional scaling.')
    distances.add_argument('--max-dimensions',
                           type=int,
                           default=10,
                           help='Maximum number of dimensions to consider '
                                'after MDS [Default: 10]')

    association = parser.add_argument_group('Association options')
    association.add_argument('--continuous',
                             action='store_true',
                             default=False,
                             help='Force continuous phenotype '
                                  '[Default: binary auto-detect]')
    association.add_argument('--lineage',
                             action='store_true',
                             help='Report lineage effects')
    association.add_argument('--lineage_clusters',
                             help='Custom clusters to use as lineages '
                                  '[Default: MDS components]')
    association.add_argument('--lineage_file',
                             default="lineage_effects.txt",
                             help='File to write lineage association to'
                                  '[Default: lineage_effects.txt]')
    association.add_argument('--burden',
                             help='VCF regions to group variants by for burden'
                                  ' testing (requires --vcf). '
                                  'Requires vcf to be indexed')

    filtering = parser.add_argument_group('Filtering options')
    filtering.add_argument('--min-af',
                           type=float,
                           default=0.01,
                           help='Minimum AF [Default: 0.01]')
    filtering.add_argument('--max-af',
                           type=float,
                           default=0.99,
                           help='Maximum AF [Default: 0.99]')
    filtering.add_argument('--filter-pvalue',
                           type=float,
                           default=1,
                           help='Prefiltering t-test pvalue threshold '
                                '[Default: 1]')
    filtering.add_argument('--lrt-pvalue',
                           type=float,
                           default=1,
                           help='Likelihood ratio test pvalue threshold '
                                '[Default: 1]')

    covariates = parser.add_argument_group('Covariates')
    covariates.add_argument('--covariates',
                            default=None,
                            help='User-defined covariates file '
                                 '(tab-delimited, no header, '
                                 'first column contains sample names)')
    covariates.add_argument('--use-covariates',
                            default=None,
                            nargs='*',
                            help='Covariates to use. Format is "2 3q 4" '
                                 '(q for quantitative) '
                                 ' [Default: load covariates but don\'t use '
                                 'them]')

    other = parser.add_argument_group('Other')
    other.add_argument('--print-samples',
                       action='store_true',
                       default=False,
                       help='Print sample lists [Default: hide samples]')
    other.add_argument('--uncompressed',
                       action='store_true',
                       default=False,
                       help='Uncompressed kmers file [Default: gzipped]')
    other.add_argument('--cpu',
                       type=int,
                       default=1,
                       help='Processes [Default: 1]')

    other.add_argument('--version', action='version',
                       version='%(prog)s '+__version__)

    return parser.parse_args()


def main():
    options = get_options()

    # check some arguments here
    if options.max_dimensions < 1:
        sys.stderr.write('Minimum number of dimensions after MDS is 1\n')
        sys.exit(1)
    if options.cpu > 1 and sys.version_info[0] < 3:
        sys.stderr.write('pyseer requires python version 3 or above ' +
                         'unless the number of threads is 1\n')
        sys.exit(1)
    if options.burden and not options.vcf:
        sys.stderr.write('Burden test can only be performed with VCF input\n')
        sys.exit(1)

    # silence warnings
    warnings.filterwarnings('ignore')
    #

    # reading phenotypes
    p = load_phenotypes(options.phenotypes, options.phenotype_col)

    # Check whether any non 0/1 phenotypes
    if not options.continuous:
        if p.values[(p.values != 0) & (p.values != 1)].size > 0:
            options.continuous = True
            sys.stderr.write("Detected continuous phenotype\n")
        else:
            sys.stderr.write("Detected binary phenotype\n")

    # reading genome distances
    if options.load_m and os.path.isfile(options.load_m):
        m = pd.read_pickle(options.load_m)
        m = m.loc[p.index]
    else:
        m = load_structure(options.distances, p, options.max_dimensions,
                           options.mds, options.cpu)
        if options.save_m:
            m.to_pickle(options.save_m + ".pkl")

    if options.max_dimensions > m.shape[1]:
        sys.stderr.write('Population MDS scaling restricted to ' +
                         '%d dimensions instead of requested %d\n' %
                         (m.shape[1],
                          options.max_dimensions))
        options.max_dimensions = m.shape[1]
    m = m.values[:, :options.max_dimensions]

    all_strains = set(p.index)

    # read covariates
    if options.covariates is not None:
        cov = load_covariates(options.covariates,
                              options.use_covariates,
                              p)
        if cov is None:
            sys.exit(1)
    else:
        cov = pd.DataFrame([])

    header = ['variant', 'af', 'filter-pvalue',
              'lrt-pvalue', 'beta', 'beta-std-err',
              'intercept'] + ['PC%d' % i
                              for i in range(1, options.max_dimensions+1)]
    if options.covariates is not None:
        header = header + [x for x in cov.columns]
    if options.lineage:
        header = header + ['lineage']
    if options.print_samples:
        header = header + ['k-samples', 'nk-samples']
    header += ['notes']
    print('\t'.join(header))

    # multiprocessing setup
    if options.cpu > 1:
        pool = Pool(options.cpu)

    # calculate null regressions once
    null_fit = fit_null(p.values, m, cov, options.continuous)
    if not options.continuous:
        firth_null = fit_null(p.values, m, cov, options.continuous, True)
    else:
        firth_null = True

    if null_fit is None or firth_null is None:
        sys.stderr.write('Could not fit null model, exiting\n')
        sys.exit(1)

    # lineage effects using null model - read BAPS clusters and fit pheno ~ lineage
    lineage_clusters = None
    lineage_dict = []
    if options.lineage:
        if options.lineage_clusters:
            lineage_clusters, lineage_dict = load_lineage(options.lineage_clusters, p)
            lineage_fit = fit_null(p.values, lineage_clusters, cov, options.continuous)
        else:
            lineage_dict = ["MDS" + str(i+1) for i in range(options.max_dimensions)]
            lineage_clusters = None
            lineage_fit = null_fit

        # Calculate, sort and print lineage effects
        lineage_wald = {}
        for lineage, slope, se in zip(lineage_dict, lineage_fit.params[1:], lineage_fit.bse[1:]):
            lineage_wald[lineage] = np.absolute(slope)/se
        with open(options.lineage_file, 'w') as lineage_out:
            for lineage, wald in sorted(lineage_wald.items(), key=operator.itemgetter(1), reverse=True):
                lineage_out.write("\t".join([lineage, str(wald)]) + "\n")

    # binary regression takes LLF as null, not full model fit
    if not options.continuous:
        null_fit = null_fit.llf

    # iterator over each kmer
    # implements maf filtering
    sample_order = []
    burden_regions = deque([])
    burden = False

    if options.kmers:
        var_type = "kmers"
        if options.uncompressed:
            infile = open(options.kmers)
        else:
            infile = gzip.open(options.kmers, 'r')
    elif options.vcf:
        var_type = "vcf"
        infile = VariantFile(options.vcf)
        if options.burden:
            burden = True
            load_burden(options.burden, burden_regions)
    else:
        # Rtab files have a header, rather than sample names accessible by row
        var_type = "Rtab"
        infile = open(options.pres)
        header = infile.readline().rstrip()
        sample_order = header.split()[1:]

    k_iter = iter_variants(p, m, cov, var_type, burden, burden_regions,
                           infile, all_strains, sample_order,
                           options.lineage, lineage_clusters,
                           options.min_af, options.max_af,
                           options.filter_pvalue,
                           options.lrt_pvalue, null_fit, firth_null,
                           options.uncompressed)

    # keep track of the number of the total number of kmers and tests
    prefilter = 0
    tested = 0
    printed = 0

    # actual association test
    if options.cpu > 1:
        # multiprocessing proceeds 1000 kmers per core at a time
        if options.continuous:
            while True:
                ret = pool.starmap(continuous,
                                   itertools.islice(k_iter,
                                                    options.cpu*1000))
                if not ret:
                    break
                for x in ret:
                    if x.prefilter:
                        prefilter += 1
                        continue
                    tested += 1
                    if x.filter:
                        continue
                    printed += 1
                    print(format_output(x,
                                        lineage_dict,
                                        options.print_samples))
        else:
            while True:
                ret = pool.starmap(binary,
                                   itertools.islice(k_iter,
                                                    options.cpu*1000))
                if not ret:
                    break
                for x in ret:
                    if x.prefilter:
                        prefilter += 1
                        continue
                    tested += 1
                    if x.filter:
                        continue
                    printed += 1
                    print(format_output(x,
                                        lineage_dict,
                                        options.print_samples))
    else:
        for data in k_iter:
            if options.continuous:
                ret = continuous(*data)
            else:
                ret = binary(*data)
            if ret.prefilter:
                prefilter += 1
                continue
            tested += 1
            if ret.filter:
                continue
            printed += 1
            print(format_output(ret,
                                lineage_dict,
                                options.print_samples))

    sys.stderr.write('%d loaded variants\n' % (prefilter + tested))
    sys.stderr.write('%d filtered variants\n' % prefilter)
    sys.stderr.write('%d tested variants\n' % tested)
    sys.stderr.write('%d printed variants\n' % printed)


if __name__ == "__main__":
    main()
