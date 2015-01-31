# ######################################################################
# Copyright (c) 2014, Brookhaven Science Associates, Brookhaven        #
# National Laboratory. All rights reserved.                            #
#                                                                      #
# Redistribution and use in source and binary forms, with or without   #
# modification, are permitted provided that the following conditions   #
# are met:                                                             #
#                                                                      #
# * Redistributions of source code must retain the above copyright     #
#   notice, this list of conditions and the following disclaimer.      #
#                                                                      #
# * Redistributions in binary form must reproduce the above copyright  #
#   notice this list of conditions and the following disclaimer in     #
#   the documentation and/or other materials provided with the         #
#   distribution.                                                      #
#                                                                      #
# * Neither the name of the Brookhaven Science Associates, Brookhaven  #
#   National Laboratory nor the names of its contributors may be used  #
#   to endorse or promote products derived from this software without  #
#   specific prior written permission.                                 #
#                                                                      #
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS  #
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT    #
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS    #
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE       #
# COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT,           #
# INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES   #
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR   #
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)   #
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,  #
# STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OTHERWISE) ARISING   #
# IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE   #
# POSSIBILITY OF SUCH DAMAGE.                                          #
########################################################################
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)
import six
from collections import namedtuple, OrderedDict, deque
import warnings
import pandas as pd
import numpy as np
from pims.base_frames import FramesSequence
from pims.frame import Frame

from skimage.io import imread


class Unalignable(Exception):
    """
    An exception to raise if you try to align a non-fillable column
    to a non-pre-aligned column
    """
    pass


class ColSpec(namedtuple(
              'ColSpec', ['name', 'dims', 'upsample', 'downsmaple'])):
    """
    Named-tuple sub-class to validate the column specifications for the
    DataMuggler

    Parameters
    ----------
    name : hashable
    dims : uint
        Dimensionality of the data stored in the column
    upsample : {None, 'ffill', 'bfill', callable}
        None means that no filling is done
    downsample : {None, 'ffill', 'bfill', callable}
        None means that no filling is done
    """
    # These reflect the 'method' argument of pandas.DataFrame.fillna
    sampling_methods = {None, 'ffill', 'pad', 'backfill', 'bfill'}

    __slots__ = ()

    def __new__(cls, name, dims, upsample, downsample):
        # sanity check dims
        if int(dims) < 0:
            raise ValueError("Dims must be positive not {}".format(dims))

        # sanity check sample method
        for sample in (upsample, downsample):
            if not (sample in cls.sampling_methods or callable(sample)):
                raise ValueError("{} is not a valid sampling method. It "
                                 "must be one of {} or callable".format(
                                     sample, cls.sampling_methods))

        # pass everything up to base class
        return super(ColSpec, cls).__new__(
            cls, name, dims, upsample, downsample)


class DataMuggler(object):
    """
    This class provides a wrapper layer of signals and slots
    around a pandas DataFrame to make plugging stuff in for live
    view easier.

    The data collection/event model being used is all measurements
    (that is values that come off of the hardware) are time stamped
    to ring time.

    The language being used through out is that of pandas data frames.

    The data model is that of a sparse table keyed on time stamps which
    is 'densified' on demand by propagating measurements forwards.  Not
    all measurements (ex images) can be filled.  This behavior is controlled
    by the `col_info` tuple.


    Parameters
    ----------
    col_info : list
        List of information about the columns. Each entry should
        be a tuple of the form (col_name, fill_method, dimensionality). See
        `ColSpec` class docstring

    max_frames : int, optional
        The maximum number of frames for the non-scalar columns

    use_pims_fs : bool, optional
        If the DM should use a pims frame-store to deal with 2D data.
        It allows file names to be added transparently, but max_frames
        won't apply to frames added as arrays.

    """
    def __init__(self, events=None):
        self.sources = {}
        self.specs = {}
        self._data = deque()
        self._time = deque()
        self._stale = True
        if events is not None:
            for event in events:
                self.append_event(event)

    @classmethod
    def from_tuples(cls, event_tuples, sources=None):
        """
        Parameters
        ----------
        event_tuples : list of (time, data_dict) tuples
            formatted like
            [(<time>: {<data_key>: <value>, <data_key>: <value>, ...}), ...]
        metatdata : dict
            mapping data keys to source names

            This information is used to look up resampling behavior.
        """
        raise NotImplementedError()
        for event in event_tuples:
            # TODO Make this look like an event object.
            pass

    @classmethod
    def from_events(cls, events):
        return cls(events)

    def append_event(self, event):
        self._stale = True
        for name, description in event.descriptor.data_keys.items():

            # If we have this source name, check for name collisions.
            if name in self.sources:
                if self.sources[name] != description['source']:
                    raise ValueError("In a previously loaded event, "
                                     "'{0}' refers to {1} but in Event "
                                     "{2} it refers to {3}.".format(
                                         name, self.sources[name],
                                         event.id,
                                         description['source']))

            # If it is a new name, determine a ColSpec.
            else:
                self.sources[name] = description['source']
                if 'external' in event.descriptor.data_keys.keys():
                    # TODO Figure out the specific dimension.
                    pass
                else:
                    dim = 0

                col_spec = ColSpec(name, dim, None, None)  # defaults
                # TODO Look up source-specific default in a config file
                # or some other source of reference data.
                col_spec = ColSpec(name, dim, 'ffill', np.mean)  # TEMP!
                self.specs[name] = col_spec

            # TODO Handle nonscalar data
            self._data.append(event.data)
            self._time.append(event.time)

    @property
    def _dataframe(self):
        # Rebuild the DataFrame if more data has been added.
        if self._stale:
            self._df = pd.DataFrame(list(self._data), index=list(self._time))
            self._stale = False
        return self._df

    def bin_by_edges(self, bin_edges):
        """
        Return data, resampled as necessary.

        Parameters
        ----------
        bin_edges : list
           list of two-element items like [(t1, t2), (t3, t4), ...]

        Returns
        -------
        data : dict of lists
        """
        binning = np.zeros(len(self._time), dtype=np.bool)
        for i, pair in enumerate(bin_edges):
            binning[(self._time < pair[0]) & (self._time > pair[1])] = i
        return self.resample(binning)

    def resample(self, binning, agg=None, interpolate=None):
        grouped = self._dataframe.groupby(binning)

        # 1. How many (non-null) data points in each bin?
        counts = grouped.count()

        # 2. If upsampling is possible, put one point in the center of
        #    each bin.
        upsampled_df = pd.DataFrame()
        for col_name in upsampled_df:
            upsampling_rule = self.upsampling_rules[col_name]
            col_data = self._dataframe[col_name]
            if upsampling_rule is None:
                # Copy the column with no changes.
                upsampled_df[col_name] = col_data
            elif upsampling_rule in ColSpec.upsampling_methods:
                # Then method must be a string.
                upsampled_df[col_name] = col_data.fillna(upsampling_rule)
            else:
                # The method is a callable. For sample, a curried
                # pandas.rolling_apply would make sense here.
                upsampled_df[col_name] = upsampling_rule(col_data)

    def add_column(self, col_info):
        """
        Adds a column to the DataMuggler

        Parameters
        ----------
        col_info : tuple
            Of the form (col_name, fill_method, dimensionality). See
           `ColSpec` class docstring
        """
        # make sure we got valid input
        col_info = ColSpec(*col_info)

        # check that the column with the same name does not exist
        if col_info.name in [c.name for c in self._col_info]:
            raise ValueError(
                "The key {} already exists in the DM".format(col_info.name))

        # stash the info for future lookup
        self._col_info.append(col_info)
        # stash the fill method
        self._col_fill[col_info.name] = col_info.fill_method
        # check if we need to deal with none-scalar data
        if col_info.dims > 0:
            self._is_col_nonscalar.add(col_info.name)
            if self._use_fs and col_info.dims == 2:
                self._framestore[col_info.name] = ImageSeq(None)
            else:
                self._nonscalar_col_lookup[col_info.name] = OrderedDict()

        self._dataframe[col_info.name] = pd.Series(np.nan,
                                                   index=self._dataframe.index)

    @property
    def col_dims(self):
        """
        The dimensionality of the data stored in all columns. Returned as a
        dictionary keyed on column name.

         0 -> scalar
         1 -> line (MCA spectra)
         2 -> image
         3 -> volume
        """
        return {c.name: c.dims for c in self._col_info}

    @property
    def ncols(self):
        """
        The number of columns that the DataMuggler contains
        """
        return len(self._col_info)

    @property
    def col_fill_rules(self):
        """
        Fill rules for all of the columns.
        """
        return {c.name: c.fill_method for c in self._col_info}

    def align_against(self, ref_col, other_cols=None):
        """
        DEPRECATED: For backward-compatibility, the returns a dict with all
        values True.
        """
        warnings.warn("align_against is deprecated; everything can be aligned")
        dict_of_truth = {col_name: True for col_name in self.keys()}
        return dict_of_truth

    def append_data(self, time_stamp, data_dict):
        """
        Add data to the DataMuggler.

        Parameters
        ----------
        time_stamp : datetime or list of datetime
            The times of the data

        data_dict : dict
            The keys must be a sub-set of the columns that the DataMuggler
            knows about.  If `time_stamp` is a list, then the values must be
            lists of the same length, if `time_stamp` is a single datatime
            object then the values must be single values
        """
        if not all(k in self._dataframe for k in data_dict):
            k_dataframe = set(list(self._dataframe.columns.values))
            k_input = set(list(six.iterkeys(data_dict)))
            bogus_keys = k_input - k_dataframe
            raise ValueError('Passing in a key that the dataframe doesn\'t '
                             'know about. Key(s): {}'.format(bogus_keys))
        try:
            iter(time_stamp)
        except TypeError:
            # if time_stamp is not iterable, assume it is a datetime object
            # and we only have one data point to deal with so up-convert
            time_stamp = [time_stamp, ]
            data_dict = {k: [v, ] for k, v in six.iteritems(data_dict)}
        else:
            # make a (shallow) copy because we will mutate the dictionary
            data_dict = dict(data_dict)

        non_scalar_keys = [k for k in data_dict
                           if k in self._is_col_nonscalar]
        # deal with non-scalar look up magic
        for k in non_scalar_keys:
            ids = []
            # if frame data, use a pims object
            if k in self._framestore:
                fs = self._framestore[k]
                for t, v in zip(time_stamp, data_dict[k]):
                    ids.append(len(fs))
                    # if it looks like a file name...
                    if isinstance(v, six.string_types):
                        fs.append_fname(v)
                    # else, assume it is an array
                    else:
                        fs.append_array(v)
            # else, dump into an ordered dict
            else:
                cl = self._nonscalar_col_lookup[k]
                for t, v in zip(time_stamp, data_dict[k]):
                    ids.append(id(v))
                    cl[(t, id(v))] = v
            data_dict[k] = ids

        # make a new data frame with the input data and append it to the
        # existing data
        df, new = self._dataframe.align(
            pd.DataFrame(data_dict, index=time_stamp))
        df.update(new)
        self._dataframe = df
        self._dataframe.sort(inplace=True)

    def get_values(self, ref_col, other_cols, t_start=None, t_finish=None):
        """
        Return a dictionary of data resampled (filled) to the times which have
        non-NaN values in the reference column

        Parameters
        ----------
        ref_col : str
            The name of the 'master' column to get time stamps from

        other_cols : list of str
            A list of column names to return data from

        t_start : datetime or None
            Start time to obtain data for. This is not implemented

        t_finish : datetime or None
            End time to obtain data for. This is not implemented

        Returns
        -------
        indices : list
            Nominally the times of each of data points

        out_data : dict
            A dictionary of the data keyed on the column name with values
            as lists whose length is the same as 'indices'
        """
        if t_start is not None:
            raise NotImplementedError("t_start is not implemented. You can "
                                      "only get all data right now")
        if t_finish is not None:
            raise NotImplementedError("t_finish is not implemented. You can "
                                      "only get all data right now")
        cols = list(set(other_cols + [ref_col, ]))
        index = self._dataframe[ref_col].dropna().index
        dense_table = self._densify_sub_df(cols)
        reduced_table = dense_table.loc[index]
        out_index, out_data = self._listify_output(reduced_table)
        # return the times/indices and the dictionary
        return out_index, out_data

    def keys(self, dim=None):
        """
        Get the column names in the data muggler

        Parameters
        ----------
        dim : int
            Select out only columns with the given dimensions

            --  ------------------
            0   scalar
            1   line (MCA spectra)
            2   image
            3   volume
            --  -------------------

        Returns
        -------
        keys : list
            Column names in the data muggler that match the desired
            dimensionality, or all column names if dim is None
        """
        cols = [c.name for c in self._col_info
                if (True if dim is None else dim == c.dims)]
        cols.sort(key=lambda s: s.lower())
        return cols

    def __iter__(self):
        return iter(self._dataframe)

    def _densify_sub_df(self, col_names, index=None):
        """
        Internal function to fill and hack-down the data frame-as-needed

        Parameters
        ----------
        col_names : list
             List of strings naming the columns to extract

        index : pandas index or None
            If None, do whole frame, else, only work on the
            subset specified by index.  This is applied _before_ filling
            so this should be a continious range (or mask out rows you don't
            want included) _not_ for reducing the result to the times based
            on a reference column.

        Returns
        -------
        DataFrame
            A filled data frame
        """
        tmp_data = dict()
        if index is not None:
            work_df = self._dataframe[index]
        else:
            work_df = self._dataframe
        for col in col_names:
            # grab the column
            work_series = work_df[col]
            # fill in the NaNs using what ever method needed
            if self._col_fill[col] is not None:
                work_series = work_series.fillna(
                    method=self._col_fill[col])
            tmp_data[col] = work_series
        return pd.DataFrame(tmp_data)

    def _listify_output(self, df):
        """
        Given a data frame (which is assumed to be a hacked-down
        version of self._dataframe which has been densified)

        This does very little validation as it is an internal function.

        This is intended to be used _after_ the data frame has been reduced
        to only the rows where the reference column has values.

        Parameters
        ----------
        df : DataFrame
            This needs to be a densified version the `_dataframe` possibly
            with a reduced number of columns.

        Returns
        -------
        index : pandas.core.index.Index
            The index of the data frame
        data : dict
            Dictionary keyed on column name of the column.  The value is
            one of (ndarray, list, pd.Series)
        """
        ret_dict = dict()
        for col in df:
            ws = df[col]
            if ws.isnull().any():
                print(col)
                print(ws)
                raise Unalignable("columns aren't aligned correctly")

            if col in self._is_col_nonscalar:
                if col in self._framestore:
                    fs = self._framestore[col]
                    ret_dict[col] = [fs[n] for n in ws]
                else:
                    lookup_dict = self._nonscalar_col_lookup[col]
                    # the iteritems generates (time, id(v))
                    # pairs
                    ret_dict[col] = [lookup_dict[t] for t
                                     in six.iteritems(ws)]
            else:
                ret_dict[col] = ws
        return df.index, ret_dict


class DmImgSequence(FramesSequence):
    """
    This is a PIMS class for dealing with images stored in a DataMuggler.

    Parameters
    ----------
    dm : DataMuggler
        Where to get the data from
    """
    @classmethod
    def class_exts(cls):
        # does not do files
        return set()

    def __init__(self, data_muggler, data_name, image_shape=None,
                 process_func=None, dtype=None, as_grey=False):
        # stash the DataMuggler
        self._data_muggler = data_muggler
        # stash the column we care about
        self._data_name = data_name
        # assume is floats (for now)
        self._pixel_type = np.float
        # frame shape is passed in
        if image_shape is None:
            image_shape = (1, 1)
        self._image_shape = image_shape

        self._validate_process_func(process_func)
        self._as_grey(as_grey, process_func)

    @property
    def data_name(self):
        return self._data_name

    @property
    def data_muggler(self):
        return self._data_muggler

    @property
    def frame_shape(self):
        return self._image_shape

    @property
    def pixel_type(self):
        return self._pixel_type

    def get_frame(self, n):
        # NOTE: This is broken by the removal of get_row
        # and get_times. Revisit later.
        ts = self._data_muggler.get_times(self.data_name)
        data = self._data_muggler.get_row(ts[n], [self.data_name, ])
        raw_data = data[self.data_name]
        self._image_shape = raw_data.shape
        return Frame(self.process_func(raw_data).astype(self._pixel_type),
                     frame_no=n)

    def __len__(self):
        return len(self._data_muggler.get_times(self.data_name))

    def __repr__(self):
        state = "Current state of DmImgSequence object"
        state += "\nData Muggler: {}".format(self._data_muggler)
        state += "\nData Name: {}".format(self._data_name)
        state += "\nPixel Type: {}".format(self._pixel_type)
        state += "\nImage Shape: {}".format(self._image_shape)
        return state


class ImageSeq(FramesSequence):
    """
    An appendable, memoized PIMS objects.

    This is to support lazy loading of files mixed with


    Parameters
    ----------
    im_shape : tuple
        The shape of the images


    """
    def __init__(self, im_shape, process_func=None, dtype=None,
                 as_grey=False, plugin=None):
        if dtype is None:
            dtype = np.uint16
        self._dtype = dtype
        self._shape = im_shape

        # cached values for fast look up, can be invalidated/cleared
        self._cache = dict()
        # dictionary, keyed on frame number of files to read data from
        self._files = dict()
        # dictionary, keyed on frame number of raw data arrays
        self._arrays = dict()

        self._count = 0

        self._validate_process_func(process_func)
        self._as_grey(as_grey, process_func)

        self.kwargs = dict(plugin=plugin)

    def __len__(self):
        return self._count

    @property
    def frame_shape(self):
        return self._shape

    @property
    def pixel_type(self):
        return self._dtype

    def get_frame(self, n):
        # first look in the cache to see if we have it
        try:
            return self._cache[n]
        except KeyError:
            pass

        # then look at the arrays, they are also fast
        try:
            return self._arrays[n]
        except:
            pass

        # finally try to open a file...if we have to
        try:
            fpath = self._files[n]
        except KeyError:
            # not sure this should ever happen
            return IndexError()

        # read the file and convert to Frame
        tmp = self._to_Frame(
            flatten_frames(fpath, self.pixel_type, self.kwargs))
        tmp.frame_no = n

        #  cache results
        self._cache[n] = tmp
        # TODO add logic to invalidate cache

        return tmp

    def append_fname(self, fname):
        """
        Add an image to the end of this sequence by adding a filename/path

        Parameters
        ----------
        fname : str
            Path to a single-frame image file.  Format must be one that
            skimage.io.imread knows how to read.

            Can handle local files + urls


        """
        self._files[self._count] = fname
        self._count += 1

    def append_array(self, img_arr):
        """
        Add an image to the end of this sequence by adding an array

        Parameters
        ----------
        img_arr : array
            Image data as an array.
        """
        tmp = self._to_Frame(img_arr)
        tmp.frame_no = self._count
        self._arrays[self._count] = tmp
        self._count += 1

    def _to_Frame(self, img):
        if img.dtype != self._dtype:
            img = img.astype(self._dtype)
        # up-convert to Frame
        return Frame(self.process_func(img))


def flatten_frames(fpath, out_dtype, read_kwargs):
    """
    Take in a multi-frame image and squash down to a single
    frame.  This is to deal with cases where at a single data
    point N frames have been collected to push the dynamic range
    of the detector.

    This is currently a place holder and only deals with single-frame files
    as @stuwilkins has not told me what types of files to expect.
    """
    # TODO dispatch logic, sum logic, basically everything
    tmp = imread(fpath, **read_kwargs).astype(out_dtype)
    return tmp
