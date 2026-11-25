# Missing-value strategy vs model metrics

8402 of 140360 (user, problem) pairs (6.0%) involve a problem whose rating was missing.

| model | F1 impute+zscore | F1 drop+zscore | dF1 | acc impute+zscore | acc drop+zscore | F1 impute, rated test rows only |
|---|---|---|---|---|---|---|
| Baseline (gap <= 0) | 0.864 | 0.876 | 0.013 | 0.871 | 0.884 | 0.877 |
| DecisionTree (entropy, ~ID3) | 0.881 | 0.892 | 0.012 | 0.884 | 0.896 | 0.893 |
| DecisionTree (gini, CART) | 0.884 | 0.895 | 0.011 | 0.886 | 0.897 | 0.896 |
| GaussianNB | 0.789 | 0.799 | 0.011 | 0.809 | 0.821 | 0.804 |
| BernoulliNB | 0.845 | 0.858 | 0.013 | 0.848 | 0.862 | 0.856 |
| SVM (RBF kernel) | 0.889 | 0.898 | 0.009 | 0.890 | 0.898 | 0.899 |
| MLP neural network | 0.889 | 0.900 | 0.010 | 0.890 | 0.901 | 0.901 |
| RandomForest | 0.889 | 0.899 | 0.010 | 0.891 | 0.901 | 0.900 |
