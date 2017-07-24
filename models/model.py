import logging
from utils.general import batches, Progbar, get_random_samples, find_best_span, save_graphs
from utils.eval import evaluate
import numpy as np
import tensorflow as tf
from os.path import join as pjoin
from abc import ABCMeta, abstractmethod


class Model(metaclass=ABCMeta):
    @abstractmethod
    def add_placeholders(self):
        pass

    @abstractmethod
    def add_preds_op(self):
        pass

    @abstractmethod
    def add_loss_op(self, preds):
        pass

    @abstractmethod
    def add_training_op(self, loss):
        pass

    @abstractmethod
    def create_feed_dict(self, data, is_train=True):
        pass

    def build(self, config, result_saver):
        self.config = config
        self.result_saver = result_saver
        self.preds = self.add_preds_op()
        self.loss = self.add_loss_op(self.preds)
        self.train_op = self.add_training_op(self.loss)

    def train(self, session, train, val):
        variables = tf.trainable_variables()
        num_vars = np.sum([np.prod(v.get_shape().as_list()) for v in variables])
        logging.info("Number of variables in models: {}".format(num_vars))
        for _ in range(self.config.num_epochs):
            self.run_epoch(session, train, val, log=self.config.log)

    def run_epoch(self, session, train, val, log):
        num_samples = len(train["context"])
        num_batches = int(np.ceil(num_samples) * 1.0 / self.config.batch_size)
        self.result_saver.save("batch_size", self.config.batch_size)

        progress = Progbar(target=num_batches)
        best_f1 = 0
        for i, train_batch in enumerate(
                batches(train, is_train=True,
                        batch_size=self.config.batch_size,
                        window_size=self.config.window_size,
                        shuffle=True)):

            _, loss = self.optimize(session, train_batch)
            progress.update(i, [("training loss", loss)])
            self.result_saver.save("losses", loss)

            if (i % self.config.eval_num == 0 and i != 0) or i == num_batches:

                # Randomly get some samples from the dataset
                train_samples = get_random_samples(train, self.config.samples_used_for_evaluation)
                val_samples = get_random_samples(val, self.config.samples_used_for_evaluation)

                # First evaluate on the training set for not using best span
                f1_train, exact_match_train = self.evaluate_answer(session, train_samples, use_best_span=False)

                # Then evaluate on the val set
                f1_val, exact_match_val = self.evaluate_answer(session, val_samples, use_best_span=False)

                if log:
                    print()
                    print("Not using best span")
                    logging.info("F1: {}, EM: {}, for {} training samples".format(f1_train, exact_match_train,
                                                                                  self.config.samples_used_for_evaluation))
                    logging.info("F1: {}, EM: {}, for {} validation samples".format(f1_val, exact_match_val,
                                                                                    self.config.samples_used_for_evaluation))

                # First evaluate on the training set
                f1_train, exact_match_train = self.evaluate_answer(session, train_samples, use_best_span=True)

                # Then evaluate on the val set
                f1_val, exact_match_val = self.evaluate_answer(session, val_samples, use_best_span=True)

                if log:
                    print()
                    print("Using best span")
                    logging.info("F1: {}, EM: {}, for {} training samples".format(f1_train, exact_match_train,
                                                                                  self.config.samples_used_for_evaluation))
                    logging.info("F1: {}, EM: {}, for {} validation samples".format(f1_val, exact_match_val,
                                                                                    self.config.samples_used_for_evaluation))

                # Now we should get the values for the loss of the training and validation sets to see how they differ
                train_loss = self.evaluate_loss(session, train_samples)
                val_loss = self.evaluate_loss(session, val_samples)

                self.result_saver.save("f1_train", f1_train)
                self.result_saver.save("EM_train", exact_match_train)
                self.result_saver.save("f1_val", f1_val)
                self.result_saver.save("EM_val", exact_match_val)
                batches_trained = 1 if self.result_saver.is_empty("batch_indices") \
                    else self.result_saver.get("batch_indices")[-1] + min(i + 1, self.config.eval_num)

                self.result_saver.save("batch_indices", batches_trained)
                self.result_saver.save("train_loss", train_loss)
                self.result_saver.save("val_loss", val_loss)

                logging.info("Finished saving the graphs!")
                save_graphs(self.result_saver.data, path=self.config.train_dir)

                if f1_val > best_f1:
                    logging.info("Saved new model!")
                    saver = tf.train.Saver()
                    saver.save(session, pjoin(self.config.train_dir, self.config.model), global_step=batches_trained)
                    best_f1 = f1_val

    def optimize(self, session, data):

        input_feed = self.create_feed_dict(data)
        output_feed = [self.train_op, self.loss]

        outputs = session.run(output_feed, input_feed)

        return outputs

    def evaluate_answer(self, session, data, use_best_span):

        # Now we whether finding the best span improves the score
        start_indicies, end_indicies = self.predict_for_batch(session, data, use_best_span)
        pred_answer, truth_answer = self.get_sentences_from_indices(data, start_indicies, end_indicies)
        result = evaluate(pred_answer, truth_answer)

        f1 = result["f1"]
        exact_match = result["EM"]

        return f1, exact_match

    def predict_for_batch(self, session, data, use_best_span):
        start_indices = []
        end_indices = []
        for batch in batches(data, is_train=False, shuffle=False):
            # logging.debug("batch is: {}".format(batch))
            start_index, end_index = self.answer(session, batch, use_best_span)
            start_indices.extend(start_index)
            end_indices.extend(end_index)
        # logging.debug("start_indices: {}".format(start_indices))
        # logging.debug("end_indices: {}".format(end_indices))
        return start_indices, end_indices

    def get_sentences_from_indices(self, data, start_index, end_index):
        answer_word_pred = []
        answer_word_truth = []
        word_context = data["word_context"]
        answer_span_start = data["answer_span_start"]
        answer_span_end = data["answer_span_end"]

        for span, context in zip(zip(start_index, end_index), word_context):
            prediction = " ".join(context.split()[span[0]:span[1] + 1])
            answer_word_pred.append(prediction)

        for span, context in zip(zip(answer_span_start, answer_span_end), word_context):
            truth = " ".join(context.split()[span[0]:span[1] + 1])
            answer_word_truth.append(truth)

        return answer_word_pred, answer_word_truth

    def evaluate_loss(self, session, data):
        losses = []
        for batch in batches(data, is_train=False, shuffle=False):
            loss = self.loss_for_batch(session, batch)
            losses.append(loss)
        return np.mean(np.array(losses))

    def loss_for_batch(self, session, data):
        input_feed = self.create_feed_dict(data)
        output_feed = self.loss

        loss = session.run(output_feed, input_feed)
        return loss

    def decode(self, session, batch_data):

        input_feed = self.create_feed_dict(batch_data, is_train=False)

        output_feed = self.preds

        start, end = session.run(output_feed, input_feed)

        return start, end

    def answer(self, session, data, use_best_span):

        start, end = self.decode(session, data)

        # logging.debug("start shape: {}".format(start.shape))
        # logging.debug("end shape: {}".format(end.shape))

        if use_best_span:
            start_index, end_index = find_best_span(start, end)
        else:
            start_index = np.argmax(start, axis=1)
            end_index = np.argmax(end, axis=1)

        # logging.debug("start_index: {}".format(start_index))
        # logging.debug("end_index: {}".format(end_index))

        return start_index, end_index
