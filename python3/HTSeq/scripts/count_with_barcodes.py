import sys
import argparse
from collections import Counter, defaultdict
import operator
import itertools
import warnings
import traceback
import os.path
import multiprocessing
import pysam

import HTSeq


class UnknownChrom(Exception):
    pass


def invert_strand(iv):
    iv2 = iv.copy()
    if iv2.strand == "+":
        iv2.strand = "-"
    elif iv2.strand == "-":
        iv2.strand = "+"
    else:
        raise ValueError("Illegal strand")
    return iv2


def count_reads_with_barcodes(
        sam_filename,
        features,
        feature_attr,
        order,
        max_buffer_size,
        stranded,
        overlap_mode,
        multimapped_mode,
        secondary_alignment_mode,
        supplementary_alignment_mode,
        feature_type,
        id_attribute,
        additional_attributes,
        quiet,
        minaqual,
        samout_format,
        samout_filename,
        nprocesses,
        cb_tag,
        ub_tag,
        ):

    def write_to_samout(r, assignment, samoutfile, template=None):
        if samoutfile is None:
            return
        if not pe_mode:
            r = (r,)
        for read in r:
            if read is not None:
                read.optional_fields.append(('XF', assignment))
                if samout_format in ('SAM', 'sam'):
                    samoutfile.write(read.get_sam_line() + "\n")
                else:
                    samoutfile.write(read.to_pysam_AlignedSegment(template))

    def identify_barcodes(r):
        '''Identify barcode from the read or pair (both must have the same)'''
        if not pe_mode:
            r = (r,)
        # cell, UMI
        barcodes = [None, None]
        nbar = 0
        for read in r:
            if read is not None:
                for tag, val in read.optional_fields:
                    if tag == cb_tag:
                        barcodes[0] = val
                        nbar += 1
                        if nbar == 2:
                            return barcodes
                    elif tag == ub_tag:
                        barcodes[1] = val
                        nbar += 1
                        if nbar == 2:
                            return barcodes
        return barcodes

    try:
        if sam_filename == "-":
            read_seq_file = HTSeq.BAM_Reader(sys.stdin)
        else:
            read_seq_file = HTSeq.BAM_Reader(sam_filename)

        # Get template for output BAM
        if samout_filename is None:
            template = None
            samoutfile = None
        elif samout_format in ('bam', 'BAM'):
            template = read_seq_file.get_template()
            samoutfile = pysam.AlignmentFile(
                    samout_filename, 'wb',
                    template=template,
                    )
        else:
            template = None
            samoutfile = open(samout_filename, 'w')

        read_seq_iter = iter(read_seq_file)
        # Catch empty BAM files
        try:
            first_read = next(read_seq_iter)
            pe_mode = first_read.paired_end
        # FIXME: catchall can hide subtle bugs
        except:
            first_read = None
            pe_mode = False
        if first_read is not None:
            read_seq = itertools.chain([first_read], read_seq_iter)
        else:
            read_seq = []
    except:
        sys.stderr.write(
            "Error occured when reading beginning of SAM/BAM file.\n")
        raise

    # CIGAR match characters (including alignment match, sequence match, and
    # sequence mismatch
    com = ('M', '=', 'X')

    try:
        if pe_mode:
            if ((supplementary_alignment_mode == 'ignore') and
               (secondary_alignment_mode == 'ignore')):
                primary_only = True
            else:
                primary_only = False
            if order == "name":
                read_seq = HTSeq.pair_SAM_alignments(
                        read_seq,
                        primary_only=primary_only)
            elif order == "pos":
                read_seq = HTSeq.pair_SAM_alignments_with_buffer(
                        read_seq,
                        max_buffer_size=max_buffer_size,
                        primary_only=primary_only)
            else:
                raise ValueError("Illegal order specified.")

        # The nesting is cell barcode, UMI, feature
        counts = defaultdict(lambda: defaultdict(Counter))
        i = 0
        for r in read_seq:
            if i > 0 and i % 100000 == 0 and not quiet:
                sys.stderr.write(
                    "%d alignment record%s processed.\n" %
                    (i, "s" if not pe_mode else " pairs"))
                sys.stderr.flush()

            i += 1

            cb, ub = identify_barcodes(r)

            if not pe_mode:
                if not r.aligned:
                    counts[cb][ub]['__not_aligned'] += 1
                    write_to_samout(
                            r, "__not_aligned", samoutfile,
                            template)
                    continue
                if ((secondary_alignment_mode == 'ignore') and
                   r.not_primary_alignment):
                    continue
                if ((supplementary_alignment_mode == 'ignore') and
                   r.supplementary):
                    continue
                try:
                    if r.optional_field("NH") > 1:
                        counts[cb][ub]['__alignment_not_unique'] += 1
                        write_to_samout(
                                r,
                                "__alignment_not_unique",
                                samoutfile,
                                template)
                        if multimapped_mode == 'none':
                            continue
                except KeyError:
                    pass
                if r.aQual < minaqual:
                    counts[cb][ub]['__too_low_aQual'] += 1
                    write_to_samout(
                            r, "__too_low_aQual", samoutfile,
                            template)
                    continue
                if stranded != "reverse":
                    iv_seq = (co.ref_iv for co in r.cigar if co.type in com
                              and co.size > 0)
                else:
                    iv_seq = (invert_strand(co.ref_iv)
                              for co in r.cigar if (co.type in com and
                                                    co.size > 0))
            else:
                if r[0] is not None and r[0].aligned:
                    if stranded != "reverse":
                        iv_seq = (co.ref_iv for co in r[0].cigar
                                  if co.type in com and co.size > 0)
                    else:
                        iv_seq = (invert_strand(co.ref_iv) for co in r[0].cigar
                                  if co.type in com and co.size > 0)
                else:
                    iv_seq = tuple()
                if r[1] is not None and r[1].aligned:
                    if stranded != "reverse":
                        iv_seq = itertools.chain(
                                iv_seq,
                                (invert_strand(co.ref_iv) for co in r[1].cigar
                                if co.type in com and co.size > 0))
                    else:
                        iv_seq = itertools.chain(
                                iv_seq,
                                (co.ref_iv for co in r[1].cigar
                                 if co.type in com and co.size > 0))
                else:
                    if (r[0] is None) or not (r[0].aligned):
                        write_to_samout(
                                r, "__not_aligned", samoutfile,
                                template)
                        counts[cb][ub]['__not_aligned'] += 1
                        continue
                if secondary_alignment_mode == 'ignore':
                    if (r[0] is not None) and r[0].not_primary_alignment:
                        continue
                    elif (r[1] is not None) and r[1].not_primary_alignment:
                        continue
                if supplementary_alignment_mode == 'ignore':
                    if (r[0] is not None) and r[0].supplementary:
                        continue
                    elif (r[1] is not None) and r[1].supplementary:
                        continue
                try:
                    if ((r[0] is not None and r[0].optional_field("NH") > 1) or
                       (r[1] is not None and r[1].optional_field("NH") > 1)):
                        write_to_samout(
                                r, "__alignment_not_unique", samoutfile,
                                template)
                        counts[cb][ub]['__alignment_not_unique'] += 1
                        if multimapped_mode == 'none':
                            continue
                except KeyError:
                    pass
                if ((r[0] and r[0].aQual < minaqual) or
                   (r[1] and r[1].aQual < minaqual)):
                    write_to_samout(
                            r, "__too_low_aQual", samoutfile,
                            template)
                    counts[cb][ub]['__too_low_aQual'] += 1
                    continue

            try:
                if overlap_mode == "union":
                    fs = set()
                    for iv in iv_seq:
                        if iv.chrom not in features.chrom_vectors:
                            raise UnknownChrom
                        for iv2, fs2 in features[iv].steps():
                            fs = fs.union(fs2)
                elif overlap_mode in ("intersection-strict",
                                      "intersection-nonempty"):
                    fs = None
                    for iv in iv_seq:
                        if iv.chrom not in features.chrom_vectors:
                            raise UnknownChrom
                        for iv2, fs2 in features[iv].steps():
                            if ((len(fs2) > 0) or
                               (overlap_mode == "intersection-strict")):
                                if fs is None:
                                    fs = fs2.copy()
                                else:
                                    fs = fs.intersection(fs2)
                else:
                    sys.exit("Illegal overlap mode.")

                if fs is None or len(fs) == 0:
                    write_to_samout(
                            r, "__no_feature", samoutfile,
                            template)
                    counts[cb][ub]['__no_feature'] += 1
                elif len(fs) > 1:
                    write_to_samout(
                            r, "__ambiguous[" + '+'.join(fs) + "]",
                            samoutfile,
                            template)
                    counts[cb][ub]['__ambiguous'] += 1
                else:
                    write_to_samout(
                            r, list(fs)[0], samoutfile,
                            template)

                if fs is not None and len(fs) > 0:
                    if multimapped_mode == 'none':
                        if len(fs) == 1:
                            counts[cb][ub][list(fs)[0]] += 1
                    elif multimapped_mode == 'all':
                        for fsi in list(fs):
                            counts[cb][ub][fsi] += 1
                    else:
                        sys.exit("Illegal multimap mode.")


            except UnknownChrom:
                write_to_samout(
                        r, "__no_feature", samoutfile,
                        template)
                counts[cb][ub]['__no_feature'] += 1

    except:
        sys.stderr.write(
            "Error occured when processing input (%s):\n" %
            (read_seq_file.get_line_number_string()))
        raise

    if not quiet:
        sys.stderr.write(
            "%d %s processed.\n" %
            (i, "alignments " if not pe_mode else "alignment pairs"))
        sys.stderr.flush()

    if samoutfile is not None:
        samoutfile.close()

    # Get rid of UMI by majority rule
    cbs = sorted(counts.keys())
    counts_noumi = {}
    for cb in cbs:
        counts_cell = Counter()
        for ub, udic in counts.pop(cb).items():
            counts_cell[udic.most_common(1)[0][0]] += 1
        counts_noumi[cb] = counts_cell

    return {
        'cell_barcodes': cbs,
        'counts': counts_noumi,
        }


def count_reads_in_features(
        sam_filename,
        gff_filename,
        order,
        max_buffer_size,
        stranded,
        overlap_mode,
        multimapped_mode,
        secondary_alignment_mode,
        supplementary_alignment_mode,
        feature_type,
        id_attribute,
        additional_attributes,
        quiet,
        minaqual,
        samout,
        samout_format,
        output_delimiter,
        output_filename,
        nprocesses,
        cb_tag,
        ub_tag,
        ):
    '''Count reads in features, parallelizing by file'''

    if samout is not None:
        # Try to open samout file early in case any of them has issues
        if samout_format in ('SAM', 'sam'):
            with open(samout, 'w'):
                pass
        else:
            # We don't have a template if the input is stdin
            if sam_filename != '-':
                with pysam.AlignmentFile(sam_filename, 'r') as sf:
                    with pysam.AlignmentFile(samout, 'w', template=sf):
                        pass

    # Try to open samfiles to fail early in case any of them is not there
    if sam_filename != '-':
        with pysam.AlignmentFile(sam_filename, 'r') as sf:
            pass

    features = HTSeq.GenomicArrayOfSets("auto", stranded != "no")
    gff = HTSeq.GFF_Reader(gff_filename)
    feature_attr = set()
    attributes = {}
    i = 0
    try:
        for f in gff:
            if f.type == feature_type:
                try:
                    feature_id = f.attr[id_attribute]
                except KeyError:
                    raise ValueError(
                            "Feature %s does not contain a '%s' attribute" %
                            (f.name, id_attribute))
                if stranded != "no" and f.iv.strand == ".":
                    raise ValueError(
                            "Feature %s at %s does not have strand information but you are "
                            "running htseq-count in stranded mode. Use '--stranded=no'." %
                            (f.name, f.iv))
                features[f.iv] += feature_id
                feature_attr.add(f.attr[id_attribute])
                attributes[f.attr[id_attribute]] = [
                        f.attr[attr] if attr in f.attr else ''
                        for attr in additional_attributes]
            i += 1
            if i % 100000 == 0 and not quiet:
                sys.stderr.write("%d GFF lines processed.\n" % i)
                sys.stderr.flush()
    except:
        sys.stderr.write(
            "Error occured when processing GFF file (%s):\n" %
            gff.get_line_number_string())
        raise

    feature_attr = sorted(feature_attr)

    if not quiet:
        sys.stderr.write("%d GFF lines processed.\n" % i)
        sys.stderr.flush()

    if len(feature_attr) == 0:
        sys.stderr.write(
            "Warning: No features of type '%s' found.\n" % feature_type)

    # Count reads
    results = count_reads_with_barcodes(
        sam_filename,
        features,
        feature_attr,
        order,
        max_buffer_size,
        stranded,
        overlap_mode,
        multimapped_mode,
        secondary_alignment_mode,
        supplementary_alignment_mode,
        feature_type,
        id_attribute,
        additional_attributes,
        quiet,
        minaqual,
        samout_format,
        samout,
        nprocesses,
        cb_tag,
        ub_tag,
        )
    # Cell barcodes
    cbs = results['cell_barcodes']
    counts = results['counts']

    # Write output
    other_features = [
        '__no_feature',
        '__ambiguous',
        '__too_low_aQual',
        '__not_aligned',
        '__alignment_not_unique',
        ]
    pad = ['' for attr in additional_attributes]
    # Header
    fields = [''] + pad + cbs
    line = output_delimiter.join(fields)
    if output_filename == '':
        print(line)
    else:
        with open(output_filename, 'w') as f:
            f.write(line)
            f.write('\n')

    # Features
    for ifn, fn in enumerate(feature_attr):
        fields = [fn] + attributes[fn] + [str(counts[cb][fn]) for cb in cbs]
        line = output_delimiter.join(fields)
        if output_filename == '':
            print(line)
        else:
            with open(output_filename, 'a') as f:
                f.write(line)
                f.write('\n')

    # Other features
    for fn in other_features:
        fields = [fn] + pad + [str(counts[cb][fn]) for cb in cbs]
        line = output_delimiter.join(fields)
        if output_filename == '':
            print(line)
        else:
            with open(output_filename, 'a') as f:
                f.write(line)
                f.write('\n')


def my_showwarning(message, category, filename, lineno=None, file=None,
                   line=None):
    sys.stderr.write("Warning: %s\n" % message)


def main():

    pa = argparse.ArgumentParser(
        usage="%(prog)s [options] alignment_file gff_file",
        description="This script takes one alignment file in SAM/BAM " +
        "format and a feature file in GFF format and calculates for each feature " +
        "the number of reads mapping to it, accounting for barcodes. See " +
        "http://htseq.readthedocs.io/en/master/count.html for details.",
        epilog="Written by Simon Anders (sanders@fs.tum.de), " +
        "European Molecular Biology Laboratory (EMBL) and Fabio Zanini " +
        "(fabio.zanini@unsw.edu.au), UNSW Sydney. (c) 2010-2020. " +
        "Released under the terms of the GNU General Public License v3. " +
        "Part of the 'HTSeq' framework, version %s." % HTSeq.__version__)

    pa.add_argument(
            "samfilename", type=str,
            help="Path to the SAM/BAM file containing the barcoded, mapped " +
            "reads. If '-' is selected, read from standard input")

    pa.add_argument(
            "featuresfilename", type=str,
            help="Path to the GTF file containing the features")

    pa.add_argument(
            "-f", "--format", dest="samtype",
            choices=("sam", "bam", "auto"), default="auto",
            help="Type of <alignment_file> data. DEPRECATED: " +
            "file format is detected automatically. This option is ignored.")

    pa.add_argument(
            "-r", "--order", dest="order",
            choices=("pos", "name"), default="name",
            help="'pos' or 'name'. Sorting order of <alignment_file> (default: name). Paired-end sequencing " +
            "data must be sorted either by position or by read name, and the sorting order " +
            "must be specified. Ignored for single-end data.")

    pa.add_argument(
            "--max-reads-in-buffer", dest="max_buffer_size", type=int,
            default=30000000,
            help="When <alignment_file> is paired end sorted by position, " +
            "allow only so many reads to stay in memory until the mates are " +
            "found (raising this number will use more memory). Has no effect " +
            "for single end or paired end sorted by name")

    pa.add_argument(
            "-s", "--stranded", dest="stranded",
            choices=("yes", "no", "reverse"), default="yes",
            help="Whether the data is from a strand-specific assay. Specify 'yes', " +
            "'no', or 'reverse' (default: yes). " +
            "'reverse' means 'yes' with reversed strand interpretation")

    pa.add_argument(
            "-a", "--minaqual", type=int, dest="minaqual",
            default=10,
            help="Skip all reads with MAPQ alignment quality lower than the given " +
            "minimum value (default: 10). MAPQ is the 5th column of a SAM/BAM " +
            "file and its usage depends on the software used to map the reads.")

    pa.add_argument(
            "-t", "--type", type=str, dest="featuretype",
            default="exon",
            help="Feature type (3rd column in GTF file) to be used, " +
            "all features of other type are ignored (default, suitable for Ensembl " +
            "GTF files: exon)")

    pa.add_argument(
            "-i", "--idattr", type=str, dest="idattr",
            default="gene_id",
            help="GTF attribute to be used as feature ID (default, " +
            "suitable for Ensembl GTF files: gene_id)")

    pa.add_argument(
            "--additional-attr", type=str,
            action='append',
            default=[],
            help="Additional feature attributes (default: none, " +
            "suitable for Ensembl GTF files: gene_name). Use multiple times " +
            "for each different attribute")

    pa.add_argument(
            "-m", "--mode", dest="mode",
            choices=("union", "intersection-strict", "intersection-nonempty"),
            default="union",
            help="Mode to handle reads overlapping more than one feature " +
            "(choices: union, intersection-strict, intersection-nonempty; default: union)")

    pa.add_argument(
            "--nonunique", dest="nonunique", type=str,
            choices=("none", "all"), default="none",
            help="Whether to score reads that are not uniquely aligned " +
            "or ambiguously assigned to features")

    pa.add_argument(
            "--secondary-alignments", dest="secondary_alignments", type=str,
            choices=("score", "ignore"), default="ignore",
            help="Whether to score secondary alignments (0x100 flag)")

    pa.add_argument(
            "--supplementary-alignments", dest="supplementary_alignments", type=str,
            choices=("score", "ignore"), default="ignore",
            help="Whether to score supplementary alignments (0x800 flag)")

    pa.add_argument(
            "-o", "--samout", type=str, dest="samout",
            default=None,
            help="Write out all SAM alignment records into a" +
            "SAM/BAM file, annotating each line " +
            "with its feature assignment (as an optional field with tag 'XF')" +
            ". See the -p option to use BAM instead of SAM.")

    pa.add_argument(
            "-p", '--samout-format', type=str, dest='samout_format',
            choices=('SAM', 'BAM', 'sam', 'bam'), default='SAM',
            help="Format to use with the --samout option."
            )

    pa.add_argument(
            "-d", '--delimiter', type=str, dest='output_delimiter',
            default='\t',
            help="Column delimiter in output (default: TAB)."
            )
    pa.add_argument(
            "-c", '--counts_output', type=str, dest='output_filename',
            default='',
            help="TSV/CSV filename to output the counts to instead of stdout."
            )

    pa.add_argument(
            "-n", '--nprocesses', type=int, dest='nprocesses',
            default=1,
            help="Number of parallel CPU processes to use (default: 1)."
            )

    pa.add_argument(
            '--cell-barcode', type=str, dest='cb_tag',
            default='CB',
            help='BAM tag used for the cell barcode (default compatible ' +
            'with 10X Genomics Chromium is CB).',
            )

    pa.add_argument(
            '--UMI', type=str, dest='ub_tag',
            default='UB',
            help='BAM tag used for the unique molecular identifier, also ' +
            ' known as molecular barcode (default compatible ' +
            'with 10X Genomics Chromium is UB).',
            )

    pa.add_argument(
            "-q", "--quiet", action="store_true", dest="quiet",
            help="Suppress progress report")  # and warnings" )

    pa.add_argument(
            "--version", action="store_true",
            help='Show software version and exit')

    args = pa.parse_args()

    if args.version:
        print(HTSeq.__version__)
        sys.exit()

    warnings.showwarning = my_showwarning
    try:
        count_reads_in_features(
                args.samfilename,
                args.featuresfilename,
                args.order,
                args.max_buffer_size,
                args.stranded,
                args.mode,
                args.nonunique,
                args.secondary_alignments,
                args.supplementary_alignments,
                args.featuretype,
                args.idattr,
                args.additional_attr,
                args.quiet,
                args.minaqual,
                args.samout,
                args.samout_format,
                args.output_delimiter,
                args.output_filename,
                args.nprocesses,
                args.cb_tag,
                args.ub_tag,
                )
    except:
        sys.stderr.write("  %s\n" % str(sys.exc_info()[1]))
        sys.stderr.write("  [Exception type: %s, raised in %s:%d]\n" %
                         (sys.exc_info()[1].__class__.__name__,
                          os.path.basename(traceback.extract_tb(
                              sys.exc_info()[2])[-1][0]),
                          traceback.extract_tb(sys.exc_info()[2])[-1][1]))
        sys.exit(1)


if __name__ == "__main__":
    main()
