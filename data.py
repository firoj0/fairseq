import os
import torch
import torch.utils.data
import numpy as np
import random
from indexed_dataset import IndexedDataset
from dictionary import Dictionary


def load(path):
    """Loads the train, valid, and test sets from the specified folder"""

    files = os.listdir(path)

    def find_languages(files):
        for filename in files:
            parts = filename.split('.')
            if parts[0] == 'train' and parts[-1] == 'idx':
                return parts[1].split('-')

    def fmt_path(fmt, *args):
        return os.path.join(path, fmt.format(*args))

    src, dst = find_languages(files)

    src_dict = Dictionary.load(fmt_path('dict.{}.txt', src))
    dst_dict = Dictionary.load(fmt_path('dict.{}.txt', dst))
    dataset = LanguageDatasets(src, dst, src_dict, dst_dict)

    for split in ['train', 'valid', 'test']:
        dataset.splits[split] = LanguagePairDataset(
            IndexedDataset(fmt_path('{0}.{1}-{2}.{1}', split, src, dst)),
            IndexedDataset(fmt_path('{0}.{1}-{2}.{2}', split, src, dst)))

    return dataset


class LanguageDatasets(object):
    def __init__(self, src, dst, src_dict, dst_dict):
        self.src = src
        self.dst = dst
        self.src_dict = src_dict
        self.dst_dict = dst_dict
        self.splits = {}

    def dataloader(self, split, epoch, batch_size=1, num_workers=0, max_len=None):
        dataset = self.splits[split]
        if split == 'train':
            batch_sampler = ShuffledBucketSampler(dataset.src, dataset.dst, batch_size, max_len)
        else:
            batch_sampler = list(batch_by_size(dataset.src, batch_size))

        return torch.utils.data.DataLoader(
            dataset,
            num_workers=num_workers,
            pin_memory=torch.cuda.is_available(),
            collate_fn=PaddingCollater(self.src_dict.index('<pad>')),
            batch_sampler=batch_sampler)


class PaddingCollater(object):
    def __init__(self, padding_value=1):
        self.padding_value = padding_value

    def __call__(self, samples):
        def merge(key, pad_begin):
            return self.merge_with_pad([s[key] for s in samples], pad_begin)

        ntokens = sum(len(s['target']) for s in samples)

        return {
            'input_tokens': merge('input_tokens', pad_begin=True),
            'input_positions': merge('input_positions', pad_begin=True),
            'target': merge('target', pad_begin=True),
            'src_tokens': merge('src_tokens', pad_begin=False),
            'src_positions': merge('src_positions', pad_begin=False),
            'ntokens': ntokens,
        }

    def merge_with_pad(self, values, pad_begin):
        size = max(v.size(0) for v in values)
        res = values[0].new(len(values), size).fill_(self.padding_value)
        for i, v in enumerate(values):
            if pad_begin:
                res[i][size-len(v):].copy_(v)
            else:
                res[i][:len(v)].copy_(v)
        return res


class LanguagePairDataset(object):
    def __init__(self, src, dst, padding_value=1, eos=2):
        self.src = src
        self.dst = dst
        self.padding_value = padding_value
        self.eos = eos

    def __getitem__(self, i):
        src = self.src[i].long() - 1
        target = self.dst[i].long() - 1
        input = target.new(target.size())
        input[0] = self.eos
        input[1:].copy_(target[:-1])

        return {
            'input_tokens': input,
            'input_positions': self.make_positions(input),
            'target': target,
            'src_tokens': src,
            'src_positions': self.make_positions(src),
        }

    def make_positions(self, x):
        start = self.padding_value + 1
        return torch.arange(start, start + len(x)).type_as(x)

    def __len__(self):
        return len(self.src)


def batch_by_size(dataset, batch_size):
    assert isinstance(dataset, IndexedDataset)
    sizes = dataset.sizes
    indices = np.argsort(sizes, kind='mergesort')

    batch = []

    def yield_batch(next_idx):
        if len(batch) == 0:
            return False
        if len(batch) == batch_size:
            return True
        return sizes[batch[0]] != sizes[next_idx]

    for idx in indices:
        if yield_batch(idx):
            yield batch
            batch = []
        batch.append(idx)

    if len(batch) > 0:
        yield batch


class ShuffledBucketSampler(object):
    """Samples from the IndexedDataset shuffled and grouped by size"""
    def __init__(self, src, dst, batch_size=1, max_len=None):
        assert isinstance(src, IndexedDataset) and isinstance(dst, IndexedDataset)
        self.src = src
        self.dst = dst
        self.batch_size = batch_size
        if max_len is None:
            max_len = float('Inf')
        self.max_len = max_len
        self.batches = None

    def __iter__(self):
        if self.batches is None:
            batches = self.shuffled_batches()
        else:
            batches = self.batches
            self.batches = None
        return iter(batches)

    def shuffled_batches(self):
        indices = np.random.permutation(len(self.src))

        # sort by sizes
        indices = indices[np.argsort(self.src.sizes[indices], kind='mergesort')]
        indices = indices[np.argsort(self.dst.sizes[indices], kind='mergesort')]

        def make_batches():
            batch = []
            seq_len = 0

            for idx in indices:
                sample_len = max(self.src.sizes[idx], self.dst.sizes[idx])
                if len(batch) > 0 and (len(batch) == self.batch_size
                                       or seq_len + sample_len > self.max_len):
                    yield batch
                    batch = []
                    seq_len = 0

                batch.append(idx)
                seq_len += sample_len

            if len(batch) > 0:
                yield batch

        batches = list(make_batches())
        random.shuffle(batches)
        return batches

    def __len__(self):
        if self.batches is None:
            self.batches = self.shuffled_batches()
        return len(self.batches)
