![](images/39c9a184bb7d67327af5bc204d620fd15d692c82063a02d7240a429c0bc49157.jpg)

<details>
<summary>area</summary>

| Metric | Value |
| --- | --- |
| Area | 0.9374 |
| Cumulative distribution | 0.9374 |
</details>

Figure 3.1 Cumulative standard normal distribution and density

So, if $d_{1} = 1.533$ , or 1.533 standard deviations, from tables of the normal distribution or using NORM.S.DIST(1.533, True) in Excel you have an area of 0.9374. This means that $93.74\%$ of the curve is contained in the region from the far left edge of the curve to $z = 1.533$ standard deviations to the right from the centre.

For $d_{2} = 1.3252$ , or 1.3252 standard deviations, less of the curve is encompassed. From probability tables or NORM.S.DIST(1.3252, True) in Excel, an area of $90.74\%$ is covered.

$$
N (d _ {1}) = \text { NORM.S.DIST } (1. 5 3 3, \text { True }) = 0. 9 3 7 4
$$

$$
N (d _ {2}) = \text { NORM.S.DIST } (1. 3 2 5 2, \text { True }) = 0. 9 0 7 4
$$

Now that you have all the inputs required, you can put them into the main Black–Scholes model.

$$
\begin{array}{l} C = S N (d _ {1}) - X e ^ {- r t} N (d _ {2}) \\ = 2 3 5 \times 0. 9 3 7 4 - 2 2 0 e ^ {- 0. 0 7 7 \times 3} \times 0. 9 0 7 4 = 6 1. 8 4. \\ \end{array}
$$

The European call option has a value of 61.84p.

The intrinsic value of the option found by comparing its exercise price with the current market price (235 - 220) = 15p.

The difference with the actual value of 61.84p is time value, reflecting the possibility over the next three years of the share price rising, making the call option more attractive.

# Activity 3.3 Calculate a Black-Scholes option price

![](images/a5ae3491c69acb2bb0e4a1ad19b089b9d82ef85e64f61a3923b9dff2b58b15bd.jpg)

Allow approximately 30 minutes for this activity.

The information about a company's shares and corresponding call options is:

<table><tr><td>Current share price (S)</td><td>473p</td></tr><tr><td>Option exercise price (X)</td><td>470p</td></tr></table>

![](images/6e89c03e273001e0e873f4591fe1ce86d80c720c56834fa7ffd0b092b925c9ef.jpg)