from __future__ import division

import os

import onmt
import onmt.Models
import onmt.modules
import argparse
import torch
import torch.nn as nn
from torch import cuda
import dill
import opts

parser = argparse.ArgumentParser(description='train.py')

# Data and loading options
parser.add_argument('-data', required=True,
                    help='Path to the *-train.pt file from preprocess.py')

# opts.py
opts.add_md_help_argument(parser)
opts.model_opts(parser)
opts.train_opts(parser)

opt = parser.parse_args()
if opt.word_vec_size != -1:
    opt.src_word_vec_size = opt.word_vec_size
    opt.tgt_word_vec_size = opt.word_vec_size

if opt.layers != -1:
    opt.enc_layers = opt.layers
    opt.dec_layers = opt.layers

opt.brnn = (opt.encoder_type == "brnn")
if opt.seed > 0:
    torch.manual_seed(opt.seed)

if torch.cuda.is_available() and not opt.gpuid:
    print("WARNING: You have a CUDA device, should run with -gpuid 0")

if opt.gpuid:
    cuda.set_device(opt.gpuid[0])
    if opt.seed > 0:
        torch.cuda.manual_seed(opt.seed)


# Set up the Crayon logging server.
if opt.exp_host != "":
    from pycrayon import CrayonClient
    cc = CrayonClient(hostname=opt.exp_host)

    experiments = cc.get_experiment_names()
    print(experiments)
    if opt.exp in experiments:
        cc.remove_experiment(opt.exp)
    experiment = cc.create_experiment(opt.exp)


def make_features(batch, fields):
    # TODO: This is bit hacky remove.
    feats = []
    for j in range(100):
        key = "src_feat_" + str(j)
        if key not in fields:
            break
        feats.append(batch.__dict__[key])
    cat = [batch.src[0]] + feats
    cat = [c.unsqueeze(2) for c in cat]
    return torch.cat(cat, 2)


def eval(model, criterion, data, fields):
    validData = onmt.IO.OrderedIterator(
        dataset=data, device=opt.gpuid[0] if opt.gpuid else -1,
        batch_size=opt.batch_size, train=False, sort=True)

    stats = onmt.Loss.Statistics()
    model.eval()
    loss = onmt.Loss.LossCompute(model.generator, criterion,
                                 fields["tgt"].vocab, data, 0, opt)
    for batch in validData:
        _, src_lengths = batch.src
        src = make_features(batch, fields)
        outputs, attn, _ = model(src, batch.tgt, src_lengths)
        gen_state = loss.makeLossBatch(outputs, batch, attn,
                                       (0, batch.tgt.size(0)))
        _, batch_stats = loss.computeLoss(batch=batch, **gen_state)
        stats.update(batch_stats)
    model.train()
    return stats


def trainModel(model, trainData, validData, fields, optim):
    model.train()

    pad_id = fields['tgt'].vocab.stoi[onmt.IO.PAD_WORD]

    # Define criterion of each GPU.
    if not opt.copy_attn:
        criterion = onmt.Loss.NMTCriterion(len(fields['tgt'].vocab), opt,
                                           pad_id)
    else:
        criterion = onmt.modules.CopyCriterion
    splitter = onmt.Loss.Splitter(opt.max_generator_batches)

    train = onmt.IO.OrderedIterator(
        dataset=trainData, batch_size=opt.batch_size,
        device=opt.gpuid[0] if opt.gpuid else -1,
        repeat=False)

    def trainEpoch(epoch):
        closs = onmt.Loss.LossCompute(model.generator, criterion,
                                      fields["tgt"].vocab, trainData,
                                      epoch, opt)

        total_stats = onmt.Loss.Statistics()
        report_stats = onmt.Loss.Statistics()

        for i, batch in enumerate(train):
            target_size = batch.tgt.size(0)

            dec_state = None
            _, src_lengths = batch.src
            src = make_features(batch, fields)
            report_stats.n_src_words += src_lengths.sum()

            # Truncated BPTT
            trunc_size = opt.truncated_decoder if opt.truncated_decoder \
                else target_size

            for j in range(0, target_size-1, trunc_size):
                # (1) Create truncated target.
                tgt_r = (j, j + trunc_size)
                tgt = batch.tgt[tgt_r[0]: tgt_r[1]]

                # (2) F-prop all but generator.

                # Main training loop
                model.zero_grad()
                outputs, attn, dec_state = \
                    model(src, tgt, src_lengths, dec_state)

                # (2) F-prop/B-prob generator in shards for memory
                # efficiency.
                batch_stats = onmt.Loss.Statistics()
                gen_state = closs.makeLossBatch(outputs, batch, attn,
                                                tgt_r)
                for shard in splitter.splitIter(gen_state):

                    # Compute loss and backprop shard.
                    loss, stats = closs.computeLoss(batch=batch,
                                                    **shard)
                    loss.div(batch.batch_size).backward()
                    batch_stats.update(stats)

                # (3) Update the parameters and statistics.
                optim.step()
                total_stats.update(batch_stats)
                report_stats.update(batch_stats)

                # If truncated, don't backprop fully.
                if dec_state is not None:
                    dec_state.detach()

            if i % opt.report_every == -1 % opt.report_every:
                report_stats.output(epoch, i+1, len(train),
                                    total_stats.start_time)
                if opt.exp_host:
                    report_stats.log("progress", experiment, optim)
                report_stats = onmt.Loss.Statistics()
        return total_stats

    for epoch in range(opt.start_epoch, opt.epochs + 1):
        print('')

        #  (1) train for one epoch on the training set
        train_stats = trainEpoch(epoch)
        print('Train perplexity: %g' % train_stats.ppl())
        print('Train accuracy: %g' % train_stats.accuracy())

        #  (2) evaluate on the validation set
        valid_stats = eval(model, criterion, validData, fields)
        print('Validation perplexity: %g' % valid_stats.ppl())
        print('Validation accuracy: %g' % valid_stats.accuracy())

        # Log to remote server.
        if opt.exp_host:
            train_stats.log("train", experiment, optim)
            valid_stats.log("valid", experiment, optim)

        #  (3) update the learning rate
        optim.updateLearningRate(valid_stats.ppl(), epoch)

        model_state_dict = (model.module.state_dict() if len(opt.gpuid) > 1
                            else model.state_dict())
        model_state_dict = {k: v for k, v in model_state_dict.items()
                            if 'generator' not in k}
        generator_state_dict = (model.generator.module.state_dict()
                                if len(opt.gpuid) > 1
                                else model.generator.state_dict())
        #  (4) drop a checkpoint
        if epoch >= opt.start_checkpoint_at:
            checkpoint = {
                'model': model_state_dict,
                'generator': generator_state_dict,
                'fields': fields,
                'opt': opt,
                'epoch': epoch,
                'optim': optim
            }
            torch.save(checkpoint,
                       '%s_acc_%.2f_ppl_%.2f_e%d.pt'
                       % (opt.save_model, valid_stats.accuracy(),
                          valid_stats.ppl(), epoch),
                       pickle_module=dill)


def check_model_path():
    save_model_path = os.path.abspath(opt.save_model)
    model_dirname = os.path.dirname(save_model_path)
    if not os.path.exists(model_dirname):
        os.makedirs(model_dirname)


def main():
    print("Loading data from '%s'" % opt.data)

    train = torch.load(opt.data + '.train.pt', pickle_module=dill)
    fields = torch.load(opt.data + '.fields.pt', pickle_module=dill)
    valid = torch.load(opt.data + '.valid.pt', pickle_module=dill)
    fields = dict(fields)
    src_features = [fields["src_feat_"+str(j)]
                    for j in range(train.nfeatures)]

    checkpoint = None
    dict_checkpoint = opt.train_from

    if dict_checkpoint:
        print('Loading dicts from checkpoint at %s' % dict_checkpoint)
        checkpoint = torch.load(dict_checkpoint,
                                map_location=lambda storage, loc: storage)
        fields = checkpoint['fields']

    print(' * vocabulary size. source = %d; target = %d' %
          (len(fields['src'].vocab), len(fields['tgt'].vocab)))
    for j, feat in enumerate(src_features):
        print(' * src feature %d size = %d' %
              (j, len(feat.vocab)))

    print(' * number of training sentences. %d' %
          len(train))
    print(' * maximum batch size. %d' % opt.batch_size)

    print('Building model...')
    cuda = (len(opt.gpuid) >= 1)
    model = onmt.Models.make_base_model(opt, opt, fields, cuda, checkpoint)
    print(model)

    if opt.train_from:
        print('Loading model from checkpoint at %s'
              % opt.train_from)
        opt.start_epoch = checkpoint['epoch'] + 1

    if len(opt.gpuid) > 1:
        print('Multi gpu training ', opt.gpuid)
        model = nn.DataParallel(model, device_ids=opt.gpuid, dim=1)
    #     generator = nn.DataParallel(generator, device_ids=opt.gpuid, dim=0)

    if not opt.train_from:
        if opt.param_init != 0.0:
            print('Intializing params')
            for p in model.parameters():
                p.data.uniform_(-opt.param_init, opt.param_init)

        model.encoder.embeddings.load_pretrained_vectors(opt.pre_word_vecs_enc)
        model.decoder.embeddings.load_pretrained_vectors(opt.pre_word_vecs_dec)

        optim = onmt.Optim(
            opt.optim, opt.learning_rate, opt.max_grad_norm,
            lr_decay=opt.learning_rate_decay,
            start_decay_at=opt.start_decay_at,
            opt=opt
        )
    else:
        print('Loading optimizer from checkpoint:')
        optim = checkpoint['optim']
        print(optim)

    optim.set_parameters(model.parameters())

    if opt.train_from:
        optim.optimizer.load_state_dict(
            checkpoint['optim'].optimizer.state_dict())

    nParams = sum([p.nelement() for p in model.parameters()])
    print('* number of parameters: %d' % nParams)
    enc = 0
    dec = 0
    for name, param in model.named_parameters():
        if 'encoder' in name:
            enc += param.nelement()
        elif 'decoder' in name:
            dec += param.nelement()
        else:
            print(name, param.nelement())
    print('encoder: ', enc)
    print('decoder: ', dec)

    check_model_path()

    trainModel(model, train, valid, fields, optim)


if __name__ == "__main__":
    main()
