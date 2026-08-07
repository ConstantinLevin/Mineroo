<table><tr><td>Risk-free rate (Rf)</td><td>6%</td></tr><tr><td>r = ln(1 + Rf) = ln(1.06)</td><td>0.058</td></tr><tr><td>Time to expiry of call option (t)</td><td>2 years</td></tr><tr><td>Volatility (standard deviation of share price, σ)</td><td>11%</td></tr></table>

Estimate the value of the call option using the Black–Scholes model.

Provide your answer...

# Feedback

In applying the Black-Scholes equation you first need to calculate the value of $d_{1}$ :

$$
\begin{array}{l} d _ {1} = \frac {1}{\sigma \sqrt {t}} \left[ \ln \left(\frac {S}{X}\right) + \left(r + \frac {\sigma^ {2}}{2}\right) t \right] \\ = \frac {1}{0 . 1 1 \sqrt {2}} \left[ \ln \left(\frac {4 7 3}{4 7 0}\right) + \left(0. 0 5 8 + \frac {0 . 1 1 ^ {2}}{2}\right) 2 \right] \\ = \frac {0 . 0 0 6 3 6 3 + 0 . 1 2 8 1}{0 . 1 5 5 6} = 0. 8 6 4 2 \\ \end{array}
$$

Next, calculate $d_{2}$ :

$$
d _ {2} = d _ {1} - \sigma \sqrt {t} = 0. 8 6 4 2 - 0. 1 1 \sqrt {2} = 0. 7 0 8 6
$$

$$
N (d _ {1}) = N (0. 8 6 4 2) = 0. 8 0 6 3
$$

$$
N (d _ {2}) = N (0. 7 0 8 6) = 0. 7 6 0 7
$$

Finally, enter these values into the call formula.

$$
\begin{array}{l} C = S N (d _ {1}) - X e ^ {- r t} N (d _ {2}) \\ = 4 7 3 \times 0. 8 0 6 3 - 4 7 0 e ^ {- 0. 0 5 8 \times 2} \times 0. 7 6 0 7 = 6 3. 0 0 9 2 \\ \end{array}
$$

The value of the call option is 63.01p.