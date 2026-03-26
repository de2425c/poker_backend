#include "turn_eval.h"

#include <algorithm>
#include <array>

#include "cards.h"

namespace {
// Check if two hole cards overlap (share any card).
bool cards_overlap(const std::array<int, 2>& a, const std::array<int, 2>& b) {
    return a[0] == b[0] || a[0] == b[1] || a[1] == b[0] || a[1] == b[1];
}

// Check if a card is blocked by board or hole cards.
bool is_blocked(int card, const std::vector<int>& board, const std::array<int, 2>& h1, const std::array<int, 2>& h2) {
    for (int bc : board) {
        if (card == bc) return true;
    }
    if (card == h1[0] || card == h1[1]) return true;
    if (card == h2[0] || card == h2[1]) return true;
    return false;
}
}  // namespace

TurnEvaluator::TurnEvaluator(const std::array<std::vector<TurnHand>, 2>& hands,
                             const std::vector<int>& board_cards)
    : hands_(hands), board_cards_(board_cards) {
    num_hands_[0] = static_cast<int>(hands_[0].size());
    num_hands_[1] = static_cast<int>(hands_[1].size());
    precompute_equities();
}

void TurnEvaluator::precompute_equities() {
    // For turn evaluation, we need to average equity over all 44 possible river cards.
    // Optimization: for each river, compute all hand strengths once, then use them
    // to determine equity for each matchup.

    int oop_count = num_hands_[0];
    int ip_count = num_hands_[1];

    // Initialize storage for equity and blocking.
    // equity_[0] stores OOP player's equity vs IP hands.
    // equity_[1] stores IP player's equity vs OOP hands.
    equity_[0].assign(static_cast<std::size_t>(oop_count) * ip_count, 0.0);
    equity_[1].assign(static_cast<std::size_t>(ip_count) * oop_count, 0.0);
    blocking_[0].assign(static_cast<std::size_t>(oop_count) * ip_count, false);
    blocking_[1].assign(static_cast<std::size_t>(ip_count) * oop_count, false);

    // Mark blocked matchups (hands that share cards).
    for (int h0 = 0; h0 < oop_count; ++h0) {
        for (int h1 = 0; h1 < ip_count; ++h1) {
            if (cards_overlap(hands_[0][h0].cards, hands_[1][h1].cards)) {
                blocking_[0][static_cast<std::size_t>(h0) * ip_count + h1] = true;
                blocking_[1][static_cast<std::size_t>(h1) * oop_count + h0] = true;
            }
        }
    }

    // Count valid rivers and accumulate equity.
    std::vector<int> river_counts_0(static_cast<std::size_t>(oop_count) * ip_count, 0);

    // Temporary strength arrays for each river.
    std::vector<Strength> oop_strengths(oop_count);
    std::vector<Strength> ip_strengths(ip_count);

    // Iterate over all possible river cards.
    for (int river = 0; river < 52; ++river) {
        // Check if river is blocked by board.
        bool river_blocked = false;
        for (int bc : board_cards_) {
            if (river == bc) {
                river_blocked = true;
                break;
            }
        }
        if (river_blocked) continue;

        // Build the 5-card board with this river.
        std::array<int, 5> full_board{{board_cards_[0], board_cards_[1],
                                       board_cards_[2], board_cards_[3], river}};

        // Compute strength for all OOP hands (that don't block river).
        for (int h = 0; h < oop_count; ++h) {
            const auto& hand = hands_[0][h];
            if (hand.cards[0] == river || hand.cards[1] == river) {
                oop_strengths[h] = {{-1, -1, -1, -1, -1, -1}};  // Invalid marker
            } else {
                std::array<int, 7> full_hand{{hand.cards[0], hand.cards[1],
                                              full_board[0], full_board[1], full_board[2],
                                              full_board[3], full_board[4]}};
                oop_strengths[h] = evaluate_7(full_hand);
            }
        }

        // Compute strength for all IP hands (that don't block river).
        for (int h = 0; h < ip_count; ++h) {
            const auto& hand = hands_[1][h];
            if (hand.cards[0] == river || hand.cards[1] == river) {
                ip_strengths[h] = {{-1, -1, -1, -1, -1, -1}};  // Invalid marker
            } else {
                std::array<int, 7> full_hand{{hand.cards[0], hand.cards[1],
                                              full_board[0], full_board[1], full_board[2],
                                              full_board[3], full_board[4]}};
                ip_strengths[h] = evaluate_7(full_hand);
            }
        }

        // Accumulate equity for each non-blocked matchup.
        for (int h0 = 0; h0 < oop_count; ++h0) {
            // Skip if OOP hand blocks river.
            if (oop_strengths[h0][0] == -1) continue;

            for (int h1 = 0; h1 < ip_count; ++h1) {
                std::size_t idx = static_cast<std::size_t>(h0) * ip_count + h1;

                // Skip blocked matchups (hands share cards).
                if (blocking_[0][idx]) continue;

                // Skip if IP hand blocks river.
                if (ip_strengths[h1][0] == -1) continue;

                river_counts_0[idx]++;

                // Compare strengths.
                const Strength& s0 = oop_strengths[h0];
                const Strength& s1 = ip_strengths[h1];

                if (s0 > s1) {
                    equity_[0][idx] += 1.0;
                } else if (s0 == s1) {
                    equity_[0][idx] += 0.5;
                }
                // Loss adds 0.
            }
        }
    }

    // Normalize equity by river count and set IP equity.
    for (int h0 = 0; h0 < oop_count; ++h0) {
        for (int h1 = 0; h1 < ip_count; ++h1) {
            std::size_t idx_0 = static_cast<std::size_t>(h0) * ip_count + h1;
            std::size_t idx_1 = static_cast<std::size_t>(h1) * oop_count + h0;

            if (!blocking_[0][idx_0] && river_counts_0[idx_0] > 0) {
                equity_[0][idx_0] /= static_cast<double>(river_counts_0[idx_0]);
                equity_[1][idx_1] = 1.0 - equity_[0][idx_0];
            }
        }
    }
}

void TurnEvaluator::showdown_values(int player,
                                    const double* opp_weights,
                                    double pot_total,
                                    double contrib_player,
                                    double* out_values) const {
    int player_count = num_hands_[player];
    int opp_count = num_hands_[1 - player];
    const double* equity = equity_[player].data();
    const std::vector<bool>& blocked = blocking_[player];

    for (int h = 0; h < player_count; ++h) {
        double total_value = 0.0;
        double total_weight = 0.0;
        std::size_t row_offset = static_cast<std::size_t>(h) * opp_count;

        for (int v = 0; v < opp_count; ++v) {
            std::size_t idx = row_offset + v;
            if (blocked[idx]) continue;

            double w = opp_weights[v];
            if (w <= 0.0) continue;

            double eq = equity[idx];
            // EV = equity * pot - (1 - equity) * contrib = equity * (pot + contrib) - contrib
            // But we need consistent formulation with river solver:
            // value = win_weight * pot + tie_weight * (pot/2) - contrib * active_weight
            // Since equity = (win + 0.5 * tie) / active, we have:
            // value = equity * pot * active_weight - contrib * active_weight (approximately)
            // More precisely: expected_chips = equity * pot - (1-equity) * contrib
            //                                = equity * pot - contrib + equity * contrib
            //                                = equity * (pot + contrib) - contrib
            // But for consistency with VectorEvaluator, use:
            // value_per_matchup = equity * pot + (1-equity) * 0 - contrib
            //                   = equity * pot - contrib (when we lose)
            // Actually: win gives pot-contrib, lose gives -contrib, tie gives pot/2 - contrib
            // So: EV = equity * (pot - contrib) + (1 - equity) * (-contrib)
            //       = equity * pot - equity * contrib - contrib + equity * contrib
            //       = equity * pot - contrib

            // Weighted contribution to total value.
            // Value = weighted_equity * pot - contrib * weight
            total_value += w * (eq * pot_total - contrib_player);
            total_weight += w;
        }

        // The river evaluator doesn't normalize by weight here, it accumulates raw values.
        // To match: out_values[h] = sum_v (weight_v * (equity * pot - contrib))
        out_values[h] = total_value;
    }
}

void TurnEvaluator::fold_values(int player,
                                const double* opp_weights,
                                double value,
                                double* out_values) const {
    int player_count = num_hands_[player];
    int opp_count = num_hands_[1 - player];
    const std::vector<bool>& blocked = blocking_[player];

    for (int h = 0; h < player_count; ++h) {
        double total_weight = 0.0;
        std::size_t row_offset = static_cast<std::size_t>(h) * opp_count;

        for (int v = 0; v < opp_count; ++v) {
            if (blocked[row_offset + v]) continue;
            total_weight += opp_weights[v];
        }

        out_values[h] = value * total_weight;
    }
}

void TurnEvaluator::valid_opp_weights(int player, const double* opp_weights, double* out_values) const {
    int player_count = num_hands_[player];
    int opp_count = num_hands_[1 - player];
    const std::vector<bool>& blocked = blocking_[player];

    for (int h = 0; h < player_count; ++h) {
        double total_weight = 0.0;
        std::size_t row_offset = static_cast<std::size_t>(h) * opp_count;

        for (int v = 0; v < opp_count; ++v) {
            if (blocked[row_offset + v]) continue;
            total_weight += opp_weights[v];
        }

        out_values[h] = total_weight;
    }
}
