#pragma once

#include <array>
#include <vector>

#include "cards.h"

// Hand structure for turn game (no precomputed strength since it depends on river).
struct TurnHand {
    std::array<int, 2> cards{};
    double weight = 1.0;
};

struct TurnAction {
    char label = 'c';
    int amount = 0;
};

struct TurnTreeNode {
    int player = -1;
    int terminal_winner = -1;
    int contrib0 = 0;
    int contrib1 = 0;
    int action_count = 0;
    std::vector<int> next;
};

struct TurnTree {
    int root = 0;
    int max_actions = 0;
    int max_depth = 0;
    std::vector<TurnTreeNode> nodes;
};

struct TurnConfig {
    std::vector<int> board_cards;  // 4 cards (turn board)
    int pot = 1000;
    int stack = 9500;
    std::vector<double> bet_sizes{0.5, 1.0};
    bool include_all_in = true;
    int max_raises = 1000;
    std::array<std::vector<std::array<int, 2>>, 2> ranges{};
    std::array<std::vector<double>, 2> range_weights{};
};

class TurnGame {
public:
    explicit TurnGame(const TurnConfig& config);

    TurnTree build_tree() const;

    int base_pot = 0;
    int stack = 0;
    std::vector<int> board_cards;  // 4 cards
    std::vector<double> bet_sizes;
    bool include_all_in = true;
    int max_raises = 1000;

    std::array<std::vector<TurnHand>, 2> hands;
    std::array<std::vector<double>, 2> hand_weights;

private:
    std::vector<TurnHand> build_hands(const std::vector<std::array<int, 2>>& hole_cards,
                                      const std::vector<double>& weights) const;
};
