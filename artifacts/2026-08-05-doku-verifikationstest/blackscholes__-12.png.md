# European put option

The value of a European put option with the same characteristics as the call can be calculated with the help of the put-call parity:

Put = Call + Bond - Share

$$
\Leftrightarrow P = C + X e ^ {- r t} - S _ {0}
$$

where:

$P =$ price of the European put option

$X =$ exercise price

$S_{0}$ = share price today

$C =$ price of the European call option.

# Activity 3.4 Calculating a European put option

![](images/c3e506e11ab6213fd93108f14dd895899d014ccce5bd9e5c0147502628faa7a8.jpg)

Allow approximately 20 minutes for this activity.

Calculate the value of a put option that has the same characteristics as the call option from Activity 3.3 (repeated below for ease).

<table><tr><td>Current share price (S)</td><td>473p</td></tr><tr><td>Option exercise price (X)</td><td>470p</td></tr><tr><td>Risk-free rate (Rf)</td><td>6%</td></tr><tr><td>r = ln(1 + Rf) = ln(1.06)</td><td>0.058</td></tr><tr><td>Time to expiry of call option (t)</td><td>2 year</td></tr><tr><td>Volatility (standard deviation of share price, σ)</td><td>11%</td></tr></table>

Provide your answer...

# Feedback

The value of a European call option from the previous activity is:

$$
C = S N (d _ {1}) - X e ^ {- r t} N (d _ {2}) = 6 3. 0 1 \mathrm{p}
$$

Using the values given above in the put-call parity gives:

$$
\begin{array}{l} P = X e ^ {- r t} - S _ {0} + C \\ = 4 7 0 e ^ {- 0. 0 5 8 \times 2} - 4 7 3 + 6 3. 0 1 = 8. 5 3 \\ \end{array}
$$

The value of the put option is 8.53p.

![](images/d501595e3e6d8c8822dab06577f7bbe209f825ddcbee6469cf488a58f0a8eb42.jpg)