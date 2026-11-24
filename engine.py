"""Game engine shared with the desktop app: question generation, series and chains,
scoring and spoken-number parsing. No UI code lives here."""
import re
from dataclasses import dataclass

import numpy as np


@dataclass
class Question:
    text: str    # what the player sees, e.g. "47 + 18" or "+ 7"
    answer: int  # the correct result (in chain mode: the new running value)


OPERATIONS = {
    "Addition (+)": "add",
    "Subtraction (\u2212)": "sub",
    "Multiplication (\u00d7)": "mul",
    "Division (\u00f7)": "div",
    "Squares (x\u00b2)": "square",
    "Square roots (\u221a)": "sqrt",
}


class QuestionGenerator:
    """Creates random questions. All results are integers, so answers can be
    spoken or typed without decimals (division and roots are always exact)."""

    def __init__(self, seed=None):
        self.rng = np.random.default_rng(seed)

    def _int(self, lo, hi):
        return int(self.rng.integers(lo, hi + 1))

    # --- standalone questions (Separate mode, and the first step of a chain) ---
    def single(self, op):
        if op == "add":
            a, b = self._int(10, 99), self._int(10, 99)
            return Question(f"{a} + {b}", a + b)
        if op == "sub":
            a, b = sorted((self._int(10, 99), self._int(10, 99)), reverse=True)
            return Question(f"{a} \u2212 {b}", a - b)
        if op == "mul":
            a, b = self._int(2, 12), self._int(2, 12)
            return Question(f"{a} \u00d7 {b}", a * b)
        if op == "div":
            b, q = self._int(2, 12), self._int(2, 12)
            return Question(f"{b * q} \u00f7 {b}", q)
        if op == "square":
            n = self._int(2, 20)
            return Question(f"{n}\u00b2", n * n)
        if op == "sqrt":
            n = self._int(2, 20)
            return Question(f"\u221a{n * n}", n)
        raise ValueError(op)

    # --- series for Separate mode (4 + 5 = 9, then 9 + 2 = 11, then 11 + 4 ...) ---
    _DIV_STARTS = [240, 360, 420, 480, 504, 540, 600, 720, 840, 960]  # many divisors
    _ROOT_STARTS = [16, 81, 256, 625, 1296, 2401, 4096, 6561, 10000, 65536]

    def series_start(self, op):
        """First question of a new series."""
        if op == "div":  # start from a number that can be divided several times
            n = int(self.rng.choice(self._DIV_STARTS))
            divs = np.arange(2, 13)
            d = int(self.rng.choice(divs[n % divs == 0]))
            return Question(f"{n} \u00f7 {d}", n // d)
        if op == "sqrt":  # start from a number whose root is again a square
            n = int(self.rng.choice(self._ROOT_STARTS))
            return Question(f"\u221a{n}", int(np.sqrt(n)))
        return self.single(op)

    def series_next(self, op, value):
        """Next question built from the PREVIOUS ANSWER using the same operation.

        Returns None when the series has run its course (the number grew too
        large, became too small, or has no exact divisor / root), and the
        caller then starts a fresh series."""
        if op == "add":
            if value > 500:
                return None
            n = self._int(2, 20)
            return Question(f"{value} + {n}", value + n)
        if op == "sub":
            if value < 5:
                return None
            n = self._int(2, min(20, value))
            return Question(f"{value} \u2212 {n}", value - n)
        if op == "mul":
            hi = min(9, 500 // value) if value >= 1 else 0
            if hi < 2:
                return None
            n = self._int(2, hi)
            return Question(f"{value} \u00d7 {n}", value * n)
        if op == "div":
            divs = np.arange(2, 13)
            divs = divs[value % divs == 0] if value > 1 else divs[:0]
            if not divs.size:
                return None
            n = int(self.rng.choice(divs))
            return Question(f"{value} \u00f7 {n}", value // n)
        if op == "square":
            if value < 2 or value > 100:
                return None
            return Question(f"{value}\u00b2", value * value)
        if op == "sqrt":
            if value < 4 or int(np.sqrt(value)) ** 2 != value:
                return None
            return Question(f"\u221a{value}", int(np.sqrt(value)))
        raise ValueError(op)

    # --- chained questions ---------------------------------------------------
    def chain_start(self):
        """First link of a chain: a small, friendly opening like '2 x 2'."""
        return self.single(str(self.rng.choice(["add", "sub", "mul"])))

    def chain_next(self, value):
        """Next link: an operation applied to the PREVIOUS ANSWER.

        The player sees only the operation ("+ 7"). We pick only operations
        that keep the result a whole number and the numbers a sane size:
          - x and x^2 are skipped when the running value is already large
          - division only offers divisors that divide the value exactly
          - sqrt only appears when the value is a perfect square
        """
        options = ["add", "sub"]
        if abs(value) <= 200:
            options.append("mul")
        if 1 < abs(value) <= 15:
            options.append("square")

        divisors = np.arange(2, 10)
        divisors = divisors[value % divisors == 0] if value != 0 else divisors[:0]
        if divisors.size:
            options.append("div")

        if value >= 4 and int(np.sqrt(value)) ** 2 == value:
            options.append("sqrt")

        op = str(self.rng.choice(options))
        if op == "add":
            n = self._int(1, 20)
            return Question(f"+ {n}", value + n)
        if op == "sub":
            n = self._int(1, 20)
            return Question(f"\u2212 {n}", value - n)
        if op == "mul":
            n = self._int(2, 9)
            return Question(f"\u00d7 {n}", value * n)
        if op == "div":
            n = int(self.rng.choice(divisors))
            return Question(f"\u00f7 {n}", value // n)
        if op == "square":
            return Question("x\u00b2", value * value)
        return Question("\u221a", int(np.sqrt(value)))


class GameSession:
    """Holds game state: mode, score, streak and the current question."""

    def __init__(self, mode, op=None, guided=True):
        # guided=True : mixed chain shows the previous answer (and your own answer on a miss)
        # guided=False: "memory" sub-mode - the screen never shows any running value,
        #               so you must carry the number in your head
        self.mode, self.op, self.guided = mode, op, guided
        self.gen = QuestionGenerator()
        self.question = None
        self.new_series = True
        self.correct = self.total = self.streak = self.best = 0

    def reset_chain(self):
        """Forget the running value; the next question starts a fresh chain."""
        self.question = None

    def next_question(self):
        if self.mode == "mixed":
            # Chain: build the next step from the correct previous answer.
            # (If the player was wrong, the chain still continues from the
            # true value, so one slip never derails the whole run.)
            self.new_series = self.question is None
            q = (self.gen.chain_start() if self.question is None
                 else self.gen.chain_next(self.question.answer))
        else:
            # Separate mode is also a series: same operation, but each question
            # starts from the previous answer. When the series can't go on
            # (number too big/small, no exact divisor...) a new one begins.
            q = None
            if self.question is not None:
                q = self.gen.series_next(self.op, self.question.answer)
            self.new_series = q is None
            if q is None:
                q = self.gen.series_start(self.op)
        self.question = q
        return q

    def submit(self, value):
        ok = value == self.question.answer
        self.total += 1
        if ok:
            self.correct += 1
            self.streak += 1
            self.best = max(self.best, self.streak)
        else:
            self.streak = 0
        return ok


# ----------------------------------------------------------------------------
# 2. SPOKEN-NUMBER PARSING
# ----------------------------------------------------------------------------
_UNITS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve "
    "thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate(
    "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if w != "_"}
# Short answers are often misheard as homophones.
_HOMOPHONES = {"to": 2, "too": 2, "for": 4, "won": 1, "ate": 8, "tree": 3}


def parse_spoken_number(text):
    """'forty-two' -> 42, 'minus 7' -> -7, '11' -> 11, unknown -> None."""
    text = text.lower().strip()
    negative = bool(re.search(r"\b(minus|negative)\b", text)) or text.startswith("-")

    digits = re.search(r"\d+", text.replace(",", ""))
    if digits:  # recognizers usually return digits already
        value = int(digits.group())
        return -value if negative else value

    if text in _HOMOPHONES:
        return _HOMOPHONES[text]

    total = current = 0
    found = False
    for word in text.replace("-", " ").split():
        if word in _UNITS:
            current += _UNITS[word]
        elif word in _TENS:
            current += _TENS[word]
        elif word == "hundred":
            current = max(current, 1) * 100
        elif word == "thousand":
            total += max(current, 1) * 1000
            current = 0
        else:
            continue
        found = True
    if not found:
        return None
    value = total + current
    return -value if negative else value
