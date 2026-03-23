# Epistemic Separation in AI-Assisted Peer Review for Theoretical Physics: A Two-Stage Protocol for Assumption-Conditional and External-Plausibility Evaluation

## Abstract
Large language models are increasingly being explored as tools for manuscript screening, referee assistance, and structured technical review. In theoretical physics, however, such systems face a distinctive epistemic failure mode: they often conflate two logically separate tasks, namely, assessing whether a manuscript's conclusions follow from its stated axioms and definitions, and assessing whether those axioms are plausible relative to incumbent physical paradigms. This conflation produces a form of probabilistic conservatism, in which high-probability continuations aligned with dominant theoretical frameworks are preferentially treated as epistemically authoritative, while nonstandard but internally structured proposals are prematurely downgraded as analogical, numerological, or underived before their internal logic has been fully audited.

This paper formalizes that problem and proposes a remedy. First, it introduces a failure taxonomy for AI-assisted review in theoretical physics, including prior-driven false negatives, analogy demotion without proof, requirement leakage from incumbent theories, and assumption-external contamination of derivation checking. Second, it proposes a two-stage protocol for AI-assisted evaluation. Stage 1, the Assumption-Conditional Derivation Audit, asks whether a manuscript's results follow from its stated postulates, definitions, and internal mathematical machinery, without importing external paradigm commitments. Stage 2, the External Plausibility and Empirical Compatibility Audit, then evaluates the physical credibility, uniqueness, and observational viability of the framework relative to established theory and data. The purpose of this separation is not to shield speculative work from criticism, but to distinguish algebraic failure from paradigm conflict and thereby reduce false negatives during preliminary review.

## 1. Introduction

### 1.1 Background
The integration of Large Language Models (LLMs) into academic publishing is accelerating, with editorial workflows increasingly exploring these systems for manuscript triage, referee assistance, and structured technical summarization. While LLMs demonstrate remarkable proficiency in parsing standard empirical research, theoretical physics presents a uniquely demanding stress test for automated evaluation. Theoretical manuscripts frequently combine formal mathematical derivation, ontological innovation, and empirical speculation. To properly evaluate such work, a reviewer must temporarily inhabit the logical architecture of the proposed theory to test its internal consistency, even if its foundational axioms diverge from established consensus. Standard predictive text systems, optimized for identifying and replicating dominant patterns in their training data, struggle to cleanly disentangle these distinct cognitive tasks.

### 1.2 Core Claim
The central thesis of this paper is that AI-assisted peer review in theoretical physics suffers from a systemic vulnerability: **epistemic collapse**. Current unconstrained LLM evaluations frequently fuse the task of internal consistency review with the task of incumbent-theory comparison into a single evaluative act. To resolve this, we propose that AI-assisted review must strictly separate **assumption-conditional derivation checking** from **external paradigm and plausibility assessment**. When an AI is asked to evaluate a paradigm-challenging manuscript in a single pass, it predictably collapses the audit of the paper's internal mathematical logic into a critique of its external ontological departures. 

### 1.3 Probabilistic Conservatism
This epistemic collapse manifests as a phenomenon we term **probabilistic conservatism**. In the context of LLM-based evaluators, probabilistic conservatism is the tendency to favor high-probability continuations that are consistent with the dominant frameworks heavily represented in the model's pre-training and post-training distributions (such as General Relativity and the Standard Model). This is not an ideological intent or programmed dogma; it is a fundamental distributional property of predictive systems. When faced with a nonstandard theoretical framework, the model's statistical weighting heavily biases it toward incumbent priors, frequently resulting in the premature rejection of internally valid derivations simply because their foundational axioms occupy a low-probability space in the training distribution.

### 1.4 Scope and Limits
It is crucial to state the boundaries of this critique. This paper does not argue that internally consistent theories should be accepted as physically true simply because their derivations hold. Furthermore, we do not claim that AI systems are uniquely flawed in this regard; human referees frequently exhibit similar cognitive conflations when evaluating heterodox physics. The narrow, actionable claim of this paper is that the quality, fairness, and utility of AI-assisted review improve measurably when internal logical entailment and external empirical plausibility are evaluated in separate, explicitly prompted stages.

## 2. The Problem: Conflation of Internal Entailment and External Prior

### 2.1 Two Distinct Review Tasks
Rigorous peer review in the exact sciences requires the execution of two fundamentally distinct epistemic duties. The first task is the evaluation of **internal logical entailment**: determining whether a manuscript's conclusions follow mathematically and logically from its own stated axioms, definitions, and postulates. This is an audit of structural closure. The second task is **external plausibility assessment**: evaluating whether the manuscript's foundational axioms are physically justified, empirically viable, and competitive with established paradigms. A theoretical framework can possess perfect internal logical entailment while relying on highly nonstandard external priors. Epistemic integrity requires that these tasks remain compartmentalized; a derivation should not be judged algebraically invalid simply because its premises are unorthodox.

### 2.2 How LLMs Conflate Them
Large Language Models, by virtue of their architecture, inherently struggle to maintain this compartmentalization. Driven by predictive loss minimization over vast training corpora dominated by consensus science, LLMs naturally collapse internal entailment and external plausibility into a single evaluative metric. When an LLM is prompted to check the mathematical derivation of a nonstandard theory, its distributional prior often overrides the local context of the manuscript. The model prematurely imports incumbent assumptions—treating them as absolute logical constraints rather than competing models—and leverages them to critique local derivations. This conflation manifests as the AI generating critiques that sound mathematically rigorous, but are in fact based on premises not entailed by the submitted framework itself.

### 2.3 Why Theoretical Physics Is Especially Vulnerable
This algorithmic conflation is particularly damaging in theoretical physics. Progress in fundamental physics frequently requires nonstandard ontology, multi-layer assumption shifts, and extensive mathematical derivation chains that temporarily suspend established models. However, the sheer statistical weight of incumbent theories like General Relativity and the Standard Model in the training data creates a massive gravitational pull in the model's latent space. Consequently, any ontological divergence triggers high-probability "correction" pathways. The model's imperative to enforce comparison with established paradigms entirely overshadows its capacity to audit local mathematical closure. As a result, theoretical physics manuscripts with novel ontologies face systemic, prior-driven false negatives before their internal mechanics are properly evaluated.

## 3. Failure Taxonomy for AI-Assisted Review
The conflation of internal entailment and external priors manifests in specific, observable failure modes during LLM-assisted manuscript evaluation. To systematically identify and mitigate these errors, we propose the following taxonomy of epistemic misfires.

### 3.1 Prior-Driven False Negatives
Because predictive systems assign high statistical weight to consensus frameworks, a manuscript presenting a nonstandard ontology will often trigger immediate dismissal. This rejection occurs not because the model has identified a formal logical or algebraic flaw in the manuscript, but because the foundational axioms occupy a low-probability region of the model's latent space. The AI evaluator substitutes a probability judgment about the premises for a rigorous audit of the derivation.

### 3.2 Analogy Demotion Without Proof
LLMs are heavily trained on science communication and pedagogical texts, where analogies are frequently employed. When a theoretical manuscript presents a rigorous mathematical isomorphism, a functional-analytic equivalence, or an effective field theory mapping, the model often defaults to this pedagogical prior. It prematurely degrades the mathematical claim to a "loose analogy" or a "heuristic" without supplying a mathematical proof of the mapping's failure. 

### 3.3 Requirement Leakage from Incumbent Theories
Requirement leakage occurs when the model imposes external constraints derived from established theories onto the new framework. For example, if a manuscript posits a discrete or fluid substrate where Lorentz invariance is only an emergent low-energy approximation, the LLM may reject the derivations by demanding exact Planck-scale Lorentz covariance—a requirement of incumbent physics, but explicitly not a requirement of the submitted axioms. The model evaluates the manuscript against external axioms rather than its internal postulates.

### 3.4 Assumption-External Contamination
During a derivation audit, an evaluator is tasked with verifying a logical conditional: *Given assumptions A and B, does conclusion C mathematically follow?* LLMs frequently contaminate this step by interrupting the logical verification to argue that assumption A is physically unlikely relative to the Standard Model. By challenging the premise during the derivation check, the model refuses to validate the logical entailment, confounding the structural audit with an ontological dispute.

### 3.5 Ambiguity Between Non-Uniqueness and Invalidity
When a theoretical manuscript maps a novel mathematical structure to a known physical parameter, LLMs often conflate *non-uniqueness* with *formal invalidity*. The model may argue that because alternative mathematical mappings could theoretically produce the same physical value, the specific derivation provided by the author is algebraically invalid. This epistemic blurring penalizes constrained, internally consistent ansatzes by treating the lack of absolute uniqueness as a failure of mathematical derivation.

### 3.6 Compliance Drift Under Conversational Pressure
A balanced taxonomy must also account for the inverse failure mode driven by post-training alignment (e.g., RLHF). If an author actively prompts the model to accept nonstandard axioms, the LLM's instruction-following tuning can trigger compliance drift. The model may over-correct, prematurely validating the physical, external truth of the theory rather than strictly bounding its approval to conditional logical entailment. Both prior-driven rejection and compliance-driven acceptance represent failures to maintain epistemic separation.

## 4. Formal Framework: Epistemic Separation

### 4.1 Principle of Review-Phase Separation
To mitigate the failure modes identified in Section 3, we propose the principle of **Review-Phase Separation**. The core tenet of this framework is that the evaluation of a theoretical manuscript must be structurally divided into sequential, isolated cognitive tasks. A manuscript should be evaluated first for conditional derivational closure, and only subsequently for paradigm plausibility and empirical adequacy. Collapsing these tasks constitutes an epistemic category error that algorithmically disadvantages nonstandard frameworks.

### 4.2 Assumption-Conditional Evaluation
The first phase of evaluation requires the AI to operate in an explicitly assumption-conditional mode. The evaluator must be prompted to treat the manuscript's axioms, definitions, and postulates as locally absolute, regardless of their truth value relative to consensus physics. Holding assumptions fixed in this manner is not an endorsement of the underlying physics; rather, it creates a closed logical sandbox within which the AI can perform a strict audit of mathematical entailment.

### 4.3 External Plausibility Evaluation
The second phase lifts the sandbox constraints and subjects the framework to external scrutiny. Only after the internal logic has been mapped and verified is the AI permitted to introduce questions of physical realism, mapping uniqueness, empirical compatibility, and comparison to established incumbent theories (e.g., General Relativity or Quantum Field Theory). This phase explicitly assesses the burden of evidence required to justify the manuscript's ontological departures.

### 4.4 Why Separation Reduces False Negatives
By enforcing this separation, the review process achieves a higher degree of epistemic resolution. If a manuscript fails, the author and human editor receive a precise localization of the failure: they learn whether the theory is mathematically broken (an algebraic failure in Phase 1) or mathematically sound but physically unlikely (an empirical conflict in Phase 2). This prevents the AI from prematurely aborting a derivation check due to a paradigm conflict, thereby drastically reducing prior-driven false negatives while maintaining rigorous critical standards.

## 5. The Two-Stage Protocol for AI-Assisted Peer Review
Translating the formal framework into operational practice requires explicit constraints on how LLMs are prompted during the review cycle. We propose the following Two-Stage Protocol.

### 5.1 Stage 1: Assumption-Conditional Derivation Audit
**Purpose:** To determine whether the manuscript's conclusions follow mathematically and logically from its own internal formalism.
**Directives for the AI Evaluator:**
* Extract and explicitly list the manuscript's foundational axioms, definitions, and postulates.
* Identify which conclusions are claimed at the theorem-level within the stated framework, and which depend on explicitly postulated structures.
* Verify the logical and algebraic steps connecting the axioms to the conclusions.
* Identify the exact location of any inferential gaps or mathematical errors.
* **Constraint:** Do not appeal to incumbent physical theories (e.g., "This violates Lorentz invariance") unless the manuscript itself explicitly claims to preserve or derive them.
**Expected Output:** A formal dependency graph and a structural audit of the mathematical derivations, classifying claims as derived, postulate-conditional, fitted, heuristic, or unsupported.

### 5.2 Stage 2: External Plausibility and Empirical Compatibility Audit
**Purpose:** To evaluate the framework as a physical theory rather than merely a formal mathematical system.
**Directives for the AI Evaluator:**
* Assess whether the foundational assumptions are physically motivated or unduly arbitrary.
* Determine if the mathematical mappings to physical observables are unique, or if alternative constructions could yield similar outputs.
* Check the framework's downstream predictions against known experimental bounds and observational data.
* Evaluate whether the theory reproduces the successes of incumbent theories in the appropriate limits (e.g., the correspondence principle).
**Expected Output:** A plausibility assessment, uniqueness critique, and empirical compatibility analysis, situating the manuscript within the broader context of established physics.

### 5.3 Standardized Prompt Templates
To operationalize this protocol, human authors and editors must override the LLM's default predictive behaviors using strict meta-prompting.

**Stage 1 Template:**
> "Treat axioms [A1–An] and definitions [D1–Dn] as absolutely given for the purpose of conditional derivation checking. Identify whether theorem [T] mathematically follows from these premises. Identify any hidden assumptions. Do not assess whether [A1–An] are physically true or aligned with the Standard Model unless a logical dependency requires it."

**Stage 2 Template:**
> "Having established the conditional logic, now assess whether axioms [A1–An] are physically justified. Determine whether theorem [T]'s mapping to physical observables is unique, and evaluate whether the overall framework is compatible with current observational data and established physical paradigms."

### 5.4 Expected Benefits
Adoption of this protocol yields several immediate benefits for the peer-review ecosystem: it ensures cleaner failure localization, generates more reproducible AI reviews across different models, provides fair treatment for nonstandard frameworks, and eliminates the common AI failure mode of confusing a nonstandard ontology with an algebraic error.

## 6. Methodological Implementation
[Placeholder for drafting]

## 7. Case Studies of Constraint-Conditioned Evaluative Shift
[Placeholder for drafting]

## 8. Discussion
[Placeholder for drafting]

## 9. Limitations
[Placeholder for drafting]

## 10. Conclusion
[Placeholder for drafting]
