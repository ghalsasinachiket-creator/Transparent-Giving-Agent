// app.js — all UI state and behaviour. Alpine calls giving() once for the page.
function giving() {
  return {
    // ----- state -----
    view: 'give',                                   // which screen is visible
    rules: { paused: false, per_gift_cap: 100, monthly_cap: 300,
             max_filing_age_years: 3, min_revenue: 50000 },
    draft: {},                                      // editable copy used by the Rules form
    intent: '',
    loading: false,
    result: null,                                   // the /propose response
    error: '',
    notice: '',
    saved: false,
    pausing: false,
    examples: ['$20 for clean drinking water', '$10 for a children’s hospital', '$25 for disaster relief'],

    // ----- startup -----
    async init() {
      // Load the donor's current rules from the server
      try {
        const res = await fetch('/rules');
        this.rules = await res.json();
        this.draft = { ...this.rules };
      } catch (e) { this.error = 'Could not load your rules. Refresh the page to try again.'; }

      // PayPal sends the donor back to "/?donated=20.00" (or ?cancelled / ?failed)
      const p = new URLSearchParams(location.search);
      if (p.get('donated')) this.notice = `Your $${p.get('donated')} donation went through. It is recorded in your ledger.`;
      else if (p.get('cancelled')) this.notice = 'You cancelled at PayPal. No money moved.';
      else if (p.get('failed')) this.notice = 'PayPal could not complete the payment. No money moved.';
      history.replaceState(null, '', '/');          // clean the URL so a refresh doesn't repeat the notice
    },

    // ----- actions -----
    async find() {
      if (!this.intent.trim() || this.loading) return;
      this.loading = true; this.error = ''; this.result = null; this.notice = '';
      try {
        const res = await fetch('/propose', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ intent: this.intent }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Request failed');
        this.result = data;
      } catch (e) {
        this.error = `The agent couldn't finish: ${e.message}`;
      } finally { this.loading = false; }
    },

    async donate(card) {
      card.busy = true; card.error = '';
      try {
        const res = await fetch('/donate', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          // A FLAG verdict needs the donor's explicit OK, which is this button press
          body: JSON.stringify({ decision_id: card.decision.id,
                                 confirmed: card.decision.verdict === 'FLAG' }),
        });
        const data = await res.json();
        // The server re-checks the rules at this moment, so this can fail even after an approval
        if (!res.ok) throw new Error(data.detail || 'Request failed');
        window.location.href = data.approve_url;     // hand over to PayPal
      } catch (e) {
        card.error = `Not sent. ${e.message}`;
        card.busy = false;
      }
    },

    async togglePause() {
      this.pausing = true;
      try {
        const res = await fetch('/pause', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ paused: !this.rules.paused }),
        });
        const data = await res.json();
        this.rules.paused = data.paused;
        this.draft.paused = data.paused;
      } finally { this.pausing = false; }
    },

    async saveRules() {
      // Always send the current paused value, or saving would silently un-pause the agent
      const body = { ...this.draft, paused: this.rules.paused };
      const res = await fetch('/rules', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (res.ok) {
        this.rules = await res.json();
        this.saved = true;
        setTimeout(() => (this.saved = false), 2500);
      }
    },

    // ----- display helpers -----
    verdict: (c) => c.decision.verdict,
    label: (c) => ({ APPROVE: 'Approved', FLAG: 'Needs your OK', BLOCK: 'Blocked' })[c.decision.verdict],
    mark: (v) => ({ APPROVE: '✓', FLAG: '!', BLOCK: '✕' })[v],
    fmtEin: (e) => e.slice(0, 2) + '-' + e.slice(2),
    orgUrl: (e) => `https://projects.propublica.org/nonprofits/organizations/${e}`,

    // One plain-language line per card: the rule that decided the verdict
    reason(c) {
      const rs = c.decision.results, v = c.decision.verdict;
      if (v === 'APPROVE') return `${rs.length} of ${rs.length} checks passed.`;
      const worst = rs.find((r) => r.verdict === v);   // first rule with the worst verdict
      return `${worst.rule}: ${worst.detail}`;
    },

    // True when the registry name isn't just a longer/shorter form of the agent's name
    differs(c) {
      const n = (s) => s.toLowerCase().replace(/[^a-z0-9]/g, '');
      const a = n(c.proposed_name), b = n(c.matched_name);
      return !(a.includes(b) || b.includes(a));
    },
  };
}