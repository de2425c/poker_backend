#pragma once

#include <array>
#include <vector>

#include "turn_game.h"
#include "turn_eval.h"

enum class TurnAlgorithm {
    CFR,
    CFR_PLUS,
    LINEAR_CFR,
    DCFR
};

struct TurnDcfrParams {
    double alpha = 1.5;
    double beta = 0.0;
    double gamma = 2.0;
};

// Build-time toggle: use double for regret/strategy buffers when needed.
#if defined(CFR_USE_DOUBLE)
using TurnCFRScalar = double;
#else
using TurnCFRScalar = float;
#endif

class TurnTrainer {
public:
    TurnTrainer(const TurnGame& game, const TurnTree& tree, TurnAlgorithm algo, TurnDcfrParams dcfr);

    void run(int iterations);
    double exploitability() const;
    bool has_infoset(int node_id) const;
    int infoset_action_count(int node_id) const;
    int infoset_hand_count(int node_id) const;
    void average_strategy(int node_id, std::vector<double>& out) const;

private:
    struct InfoSet {
        int action_count = 0;
        int hand_count = 0;
        std::vector<TurnCFRScalar> regret;
        std::vector<TurnCFRScalar> strategy_sum;
    };

    struct ScratchFrame {
        std::vector<double> values;
        std::vector<double> strategy;
        std::vector<double> next_reach;
        std::vector<double> action_values;
    };

    const TurnGame& game_;
    const TurnTree& tree_;
    TurnEvaluator evaluator_;
    TurnAlgorithm algo_;
    TurnDcfrParams dcfr_;
    int iteration_ = 0;
    std::array<int, 2> num_hands_{{0, 0}};
    std::array<const double*, 2> hand_weights_ptr_{{nullptr, nullptr}};
    double regret_weight_ = 1.0;
    double avg_weight_ = 1.0;
    double dcfr_pos_scale_ = 1.0;
    double dcfr_neg_scale_ = 1.0;
    double dcfr_strat_scale_ = 1.0;

    std::vector<InfoSet> infosets_;
    mutable std::vector<ScratchFrame> scratch_;

    void compute_avg_strategy(const InfoSet& info, double* out_strategy) const;
    const double* best_response(int node_id,
                                int target_player,
                                const double* reach_opp,
                                int depth) const;
    double best_response_value(int target_player) const;

    const double* traverse(int node_id,
                           int update_player,
                           const double* reach_p,
                           const double* reach_opp,
                           int depth);

    void compute_strategy(const InfoSet& info, double* out_strategy) const;
    void apply_dcfr_discount(InfoSet& info, double pos_scale, double neg_scale, double strat_scale) const;
};
