const { useState, useMemo, useEffect, useRef } = React;

// ─── Embedded Data ───
const RAW_DATA = __RAW_DATA_JSON__;

const ENTITIES = __ENTITIES_JSON__;
const ENTITY_SHORT = __ENTITY_SHORT_JSON__;
const ENTITY_COLORS = __ENTITY_COLORS_JSON__;

// ─── Helpers ───

function isReadonly(d) { return __READONLY_CHECK__; }

function coordScore(d) {
  let s = 0;
  if (d.not === "Yes") s++;
  if (d.tp === "Yes") s++;
  if (d.wit === "Yes") s++;
  if (d.sc >= 3) s++;
  return s;
}

function computeCost(d, floor, ceil) {
  if (isReadonly(d)) return 0;
  const maxC = __MAX_C__, maxN = __MAX_N__;
  const cNorm = Math.min(d.c / maxC, 1);
  const nNorm = Math.min(d.ni / maxN, 1);
  const w = 0.6 * cNorm + 0.4 * nNorm;
  return Math.round(floor + w * (ceil - floor));
}

function fmt(n) {
  if (n >= 1e6) return "$" + (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return "$" + (n / 1e3).toFixed(0) + "K";
  return "$" + n;
}
function cxScore(c) { return Math.round(Math.min(c / 70, 1) * 100); }
function fmtComma(n) { return n.toLocaleString("en-US"); }
function parseComma(s) { return parseInt(s.replace(/,/g, ""), 10) || 0; }

function fmtFull(n) {
  return "$" + n.toLocaleString();
}

// ─── Components ───

const RiskDot = ({ c, ni, maxC = __MAX_C__, maxN = __MAX_N__, size = 6 }) => {
  const x = (c / maxC) * 100;
  const y = 100 - (ni / maxN) * 100;
  return <circle cx={`${x}%`} cy={`${y}%`} r={size} />;
};

function __COMP_NAME__Dashboard() {
  const [floor, setFloor] = useState(50000);
  const [ceil, setCeil] = useState(250000);
  const [floorText, setFloorText] = useState(fmtComma(50000));
  const [ceilText, setCeilText] = useState(fmtComma(250000));
  const [editingFloor, setEditingFloor] = useState(false);
  const [editingCeil, setEditingCeil] = useState(false);
  const [activeEntity, setActiveEntity] = useState("All");
  const [view, setView] = useState("overview");
  const [selectedForm, setSelectedForm] = useState(null);

  const data = useMemo(() => {
    const filtered = activeEntity === "All" ? RAW_DATA : RAW_DATA.filter(d => d.e === activeEntity);
    return filtered.map(d => ({ ...d, cost: computeCost(d, floor, ceil), readonly: isReadonly(d), coord: coordScore(d) }));
  }, [floor, ceil, activeEntity]);

  const active = useMemo(() => data.filter(d => !d.readonly), [data]);
  const totalCost = useMemo(() => active.reduce((s, d) => s + d.cost, 0), [active]);

  const entityBreakdown = useMemo(() =>
    ENTITIES.map(e => {
      const ef = data.filter(d => d.e === e);
      const ea = ef.filter(d => !d.readonly);
      return {
        name: e, short: ENTITY_SHORT[e], total: ef.length, active: ea.length,
        readonly: ef.length - ea.length,
        cost: ea.reduce((s, d) => s + d.cost, 0),
        avgComplexity: ea.length ? Math.round(ea.reduce((s, d) => s + cxScore(d.c), 0) / ea.length) : 0,
        avgNigo: ea.length ? Math.round(ea.reduce((s, d) => s + d.ni, 0) / ea.length) : 0,
      };
    }), [data]);

  const hardest = useMemo(() =>
    [...active].sort((a, b) => b.cost - a.cost).slice(0, 12), [active]);

  const highCoord = useMemo(() =>
    [...active].filter(d => d.coord >= 3).sort((a, b) => b.coord - a.coord || b.cost - a.cost), [active]);

  const conditional = useMemo(() =>
    [...active].filter(d => d.con === "Yes").sort((a, b) => b.cost - a.cost).slice(0, 15), [active]);

  const notaryForms = active.filter(d => d.not === "Yes");
  const thirdParty = active.filter(d => d.tp === "Yes");
  const witnessForms = active.filter(d => d.wit === "Yes");

  // Quadrant analysis — medians auto-calibrated from input data
  const medC = __MED_C__, medN = __MED_N__;
  const q1 = active.filter(d => d.c >= medC && d.ni >= medN);
  const q2 = active.filter(d => d.c >= medC && d.ni < medN);
  const q3 = active.filter(d => d.c < medC && d.ni >= medN);
  const q4 = active.filter(d => d.c < medC && d.ni < medN);

  return (
    <div style={{ minHeight: '100vh', background: '#FAFAF8', fontFamily: "'Poppins', 'Helvetica Neue', 'Segoe UI', Arial, sans-serif" }}>
    <style>{`
      @import url('https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;500;600;700&display=swap');
      @media (max-width: 640px) {
        .fgp-header { padding: 10px 16px !important; }
        .fgp-header img { height: 44px !important; }
        .fgp-content { padding: 16px !important; }
        .fgp-cost-bar { flex-direction: column !important; gap: 8px !important; padding: 8px 16px !important; }
        .fgp-cost-bar label { width: 100% !important; justify-content: space-between !important; }
        .fgp-cost-bar .fgp-portfolio { margin-left: 0 !important; margin-top: 4px !important; }
        .fgp-tabs { overflow-x: auto !important; -webkit-overflow-scrolling: touch !important; }
        .fgp-tabs button { padding: 10px 14px !important; white-space: nowrap !important; }
        .fgp-entity-select { font-size: 11px !important; }
        .fgp-stat-line { flex-direction: column !important; gap: 2px !important; }
        .fgp-quadrants { grid-template-columns: 1fr 1fr !important; }
        .fgp-table-wrap { font-size: 11px !important; }
        .fgp-table-wrap th, .fgp-table-wrap td { padding: 6px 8px !important; }
        .fgp-table-wrap .fgp-hide-mobile { display: none !important; }
        .fgp-footer { flex-direction: column !important; gap: 4px !important; text-align: center !important; }
        .fgp-coord-item { gap: 8px !important; }
        .fgp-modal-body { max-width: 92vw !important; margin: 16px !important; max-height: 90vh !important; }
        .fgp-modal-grid { grid-template-columns: 1fr !important; }
      }
    `}</style>

    {/* FORM DETAIL MODAL */}
    {selectedForm && (
      <div onClick={() => setSelectedForm(null)} style={{ position: 'fixed', inset: 0, zIndex: 1000, background: 'rgba(0,0,0,0.35)', display: 'flex', alignItems: 'center', justifyContent: 'center', backdropFilter: 'blur(2px)' }}>
        <div className="fgp-modal-body" onClick={e => e.stopPropagation()} style={{ background: '#FFFFFF', maxWidth: 640, width: '100%', maxHeight: '80vh', overflowY: 'auto', margin: 32, position: 'relative' }}>
          {/* Header */}
          <div style={{ padding: '24px 28px 16px', borderBottom: '1px solid #E8E5E0' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 16 }}>
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: 10, fontWeight: 600, color: '#8C8C8C', letterSpacing: 1, textTransform: 'uppercase', marginBottom: 6 }}>{ENTITY_SHORT[selectedForm.e]}</div>
                <div style={{ fontSize: 17, fontWeight: 500, color: '#1A1A1A', lineHeight: 1.35 }}>{selectedForm.n}</div>
              </div>
              <button onClick={() => setSelectedForm(null)} style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 4, color: '#8C8C8C', fontSize: 18, lineHeight: 1 }}>×</button>
            </div>
            <div style={{ marginTop: 10, fontSize: 13, color: '#4A4A4A', lineHeight: 1.5 }}>{selectedForm.pur}</div>
          </div>

          {/* Key Metrics */}
          <div style={{ padding: '16px 28px', borderBottom: '1px solid #F0EDE8' }}>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '12px 24px' }}>
              <div><span style={{ fontSize: 22, fontWeight: 300, color: '__COLOR_ACCENT__' }}>{fmt(selectedForm.cost)}</span><span style={{ fontSize: 10, color: '#8C8C8C', marginLeft: 6 }}>est. cost</span></div>
              <div style={{ borderLeft: '1px solid #E8E5E0', paddingLeft: 24 }}><span style={{ fontSize: 22, fontWeight: 300, color: '#1A1A1A' }}>{selectedForm.p}</span><span style={{ fontSize: 10, color: '#8C8C8C', marginLeft: 6 }}>pages</span></div>
              <div style={{ borderLeft: '1px solid #E8E5E0', paddingLeft: 24 }}><span style={{ fontSize: 22, fontWeight: 300, color: '#1A1A1A' }}>{selectedForm.f}</span><span style={{ fontSize: 10, color: '#8C8C8C', marginLeft: 6 }}>fields</span></div>
            </div>
          </div>

          {/* Complexity Attributes */}
          <div style={{ padding: '20px 28px' }}>
            <div style={{ fontSize: 10, fontWeight: 600, color: '#8C8C8C', letterSpacing: 1, textTransform: 'uppercase', marginBottom: 12 }}>Complexity Attributes</div>
            <div className="fgp-modal-grid" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 0 }}>
              {[
                { label: 'Complexity Score', value: cxScore(selectedForm.c) + ' / 100', flag: cxScore(selectedForm.c) > 70 ? 'high' : cxScore(selectedForm.c) > 40 ? 'med' : 'low' },
                { label: 'NIGO Rate', value: selectedForm.ni + '%', flag: selectedForm.ni > __NIGO_HIGH__ ? 'high' : selectedForm.ni > __NIGO_MED__ ? 'med' : 'low' },
                { label: 'Signature Count', value: selectedForm.sc, flag: selectedForm.sc >= 5 ? 'high' : selectedForm.sc >= 3 ? 'med' : 'low' },
                { label: 'Attachment Count', value: selectedForm.ac, flag: selectedForm.ac >= 5 ? 'high' : selectedForm.ac >= 2 ? 'med' : 'low' },
                { label: 'Coordination Score', value: selectedForm.coord + ' / 5', flag: selectedForm.coord >= 4 ? 'high' : selectedForm.coord >= 3 ? 'med' : 'low' },
                { label: 'Payer', value: selectedForm.sv, flag: 'neutral' },
              ].map((attr, i) => (
                <div key={i} style={{ padding: '10px 0', borderBottom: '1px solid #F5F3F0', display: 'flex', justifyContent: 'space-between', alignItems: 'center', paddingRight: i % 2 === 0 ? 20 : 0, paddingLeft: i % 2 === 1 ? 20 : 0, borderLeft: i % 2 === 1 ? '1px solid #F5F3F0' : 'none' }}>
                  <span style={{ fontSize: 12, color: '#4A4A4A' }}>{attr.label}</span>
                  <span style={{ fontSize: 13, fontWeight: 600, color: attr.flag === 'high' ? '#9B2335' : attr.flag === 'med' ? '#B8860B' : attr.flag === 'neutral' ? '#4A4A4A' : '__COLOR_ACCENT__' }}>{attr.value}</span>
                </div>
              ))}
            </div>
          </div>

          {/* Requirements Checklist */}
          <div style={{ padding: '0 28px 24px' }}>
            <div style={{ fontSize: 10, fontWeight: 600, color: '#8C8C8C', letterSpacing: 1, textTransform: 'uppercase', marginBottom: 10 }}>Requirements</div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
              {[
                { key: 'sig', label: 'Signature', active: selectedForm.sig === 'Yes' },
                { key: 'not', label: 'Notarization', active: selectedForm.not === 'Yes' },
                { key: 'wit', label: 'Witness', active: selectedForm.wit === 'Yes' },
                { key: 'tp', label: '3rd-Party Signer', active: selectedForm.tp === 'Yes' },
                { key: 'att', label: 'Attachments', active: selectedForm.att === 'Yes' },
                { key: 'id', label: 'ID Verification', active: selectedForm.id === 'Yes' },
                { key: 'pay', label: 'Payment', active: selectedForm.pay === 'Yes' },
                { key: 'con', label: 'Conditional', active: selectedForm.con === 'Yes' },
                { key: 'dl', label: 'Deadline', active: selectedForm.dl === 'Yes' },
              ].map(req => (
                <span key={req.key} style={{ fontSize: 11, padding: '4px 10px', background: req.active ? '__COLOR_ACCENT__' : '#F5F3F0', color: req.active ? '#FFFFFF' : '#B0AAA0', fontWeight: req.active ? 500 : 400, letterSpacing: 0.3 }}>
                  {req.label}
                </span>
              ))}
            </div>
          </div>

          {/* Form Type */}
          <div style={{ padding: '12px 28px', borderTop: '1px solid #F0EDE8', background: '#FAFAF8', display: 'flex', justifyContent: 'space-between', fontSize: 11, color: '#8C8C8C' }}>
            <span>{selectedForm.at}</span>
            <span>{selectedForm.e}</span>
          </div>
        </div>
      </div>
    )}

    {/* HEADER */}
    <div className="fgp-header" style={{ background: '__COLOR_PRIMARY__', padding: '12px 24px' }}>
      <div style={{ maxWidth: 1200, margin: '0 auto', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start', gap: 0 }}>
          <div style={{ overflow: 'hidden', height: __LOGO_CONTAINER_H__, flexShrink: 0 }}>
            <img src="__LOGO_URL__" alt="__INST_NAME__" style={{ height: __LOGO_HEIGHT__, marginTop: __LOGO_MARGIN_TOP__ }} />
          </div>
          <div style={{ color: 'rgba(255,255,255,0.7)', fontSize: 11, letterSpacing: 0.3, marginTop: -2 }}>Form Experience Conversion Analysis</div>
        </div>
        <div style={{ textAlign: 'right', fontSize: 11, color: 'rgba(255,255,255,0.55)', letterSpacing: 0.3 }}>
          <div>Prepared by DocuSign</div>
          <div>March 2026</div>
        </div>
      </div>
    </div>

    {/* COST INPUTS */}
    <div style={{ background: '#FFFFFF', borderBottom: '1px solid #E8E5E0', padding: '10px 24px' }}>
      <div className="fgp-cost-bar" style={{ maxWidth: 1200, margin: '0 auto', display: 'flex', alignItems: 'center', gap: 24, flexWrap: 'wrap' }}>
        <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'text' }}>
          <span style={{ fontSize: 11, color: '#8C8C8C', letterSpacing: 0.5, textTransform: 'uppercase' }}>Floor</span>
          <span style={{ fontSize: 11, color: '#8C8C8C' }}>$</span>
          <input type="text" value={editingFloor ? floorText : fmtComma(floor)}
            onFocus={e => { setEditingFloor(true); setFloorText(fmtComma(floor)); e.target.select(); }}
            onChange={e => {
              const txt = e.target.value;
              setFloorText(txt);
              const v = parseComma(txt);
              if (v >= 0 && v <= 10000000 && v < ceil) setFloor(v);
            }}
            onKeyDown={e => { if (e.key === 'Enter') e.target.blur(); }}
            onBlur={() => { setEditingFloor(false); const v = parseComma(floorText); if (v >= 0 && v <= 10000000) setFloor(Math.min(v, ceil - 1000)); }}
            style={{ width: 100, padding: '4px 8px', fontSize: 13, fontWeight: 600, color: '__COLOR_ACCENT__', border: 'none', borderBottom: '1.5px dashed #C8C3BA', background: 'transparent', textAlign: 'right', outline: 'none', cursor: 'text' }} />
          <svg width="11" height="11" viewBox="0 0 16 16" fill="none" style={{ opacity: 0.35, flexShrink: 0 }}><path d="M11.5 1.5l3 3L5 14H2v-3L11.5 1.5z" stroke="#8C8C8C" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/></svg>
        </label>
        <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'text' }}>
          <span style={{ fontSize: 11, color: '#8C8C8C', letterSpacing: 0.5, textTransform: 'uppercase' }}>Ceiling</span>
          <span style={{ fontSize: 11, color: '#8C8C8C' }}>$</span>
          <input type="text" value={editingCeil ? ceilText : fmtComma(ceil)}
            onFocus={e => { setEditingCeil(true); setCeilText(fmtComma(ceil)); e.target.select(); }}
            onChange={e => {
              const txt = e.target.value;
              setCeilText(txt);
              const v = parseComma(txt);
              if (v >= 1000 && v <= 100000000 && v > floor) setCeil(v);
            }}
            onKeyDown={e => { if (e.key === 'Enter') e.target.blur(); }}
            onBlur={() => { setEditingCeil(false); const v = parseComma(ceilText); if (v >= 1000 && v <= 100000000) setCeil(Math.max(v, floor + 1000)); }}
            style={{ width: 100, padding: '4px 8px', fontSize: 13, fontWeight: 600, color: '__COLOR_ACCENT__', border: 'none', borderBottom: '1.5px dashed #C8C3BA', background: 'transparent', textAlign: 'right', outline: 'none', cursor: 'text' }} />
          <svg width="11" height="11" viewBox="0 0 16 16" fill="none" style={{ opacity: 0.35, flexShrink: 0 }}><path d="M11.5 1.5l3 3L5 14H2v-3L11.5 1.5z" stroke="#8C8C8C" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/></svg>
        </label>
        <div className="fgp-portfolio" style={{ marginLeft: 'auto', display: 'flex', alignItems: 'baseline', gap: 8 }}>
          <span style={{ fontSize: 11, color: '#8C8C8C', textTransform: 'uppercase', letterSpacing: 0.5 }}>Portfolio</span>
          <span style={{ fontSize: 22, fontWeight: 300, color: '__COLOR_NAV__' }}>{fmt(totalCost)}</span>
          <span style={{ fontSize: 11, color: '#8C8C8C' }}>{active.length} forms</span>
        </div>
      </div>
    </div>

    {/* TABS */}
    <div className="fgp-tabs" style={{ background: '#FFFFFF', borderBottom: '1px solid #E8E5E0' }}>
      <div style={{ maxWidth: 1200, margin: '0 auto', padding: '0 24px' }}>
        <div style={{ display: 'flex', gap: 0 }}>
          {[["overview","Overview"],["hardest","Costliest"],["coordination","Coordination"],["conditionality","Conditionality"],["calculations","Calculations"]].map(([k,l]) => (
            <button key={k} onClick={() => setView(k)}
              style={{ padding: '12px 20px', fontSize: 12, fontWeight: view === k ? 600 : 400, color: view === k ? '__COLOR_NAV__' : '#8C8C8C', background: 'none', border: 'none', borderBottom: view === k ? '2px solid __COLOR_PRIMARY__' : '2px solid transparent', cursor: 'pointer', letterSpacing: 0.3, transition: 'all 0.2s' }}>
              {l}
            </button>
          ))}
        </div>
      </div>
    </div>

    {/* OVERVIEW TAB */}
    {view === "overview" && (
      <div className="fgp-content" style={{ maxWidth: 1200, margin: '0 auto', padding: '24px 24px' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
          <div style={{ fontSize: 10, fontWeight: 600, color: '#8C8C8C', letterSpacing: 1, textTransform: 'uppercase' }}>Portfolio Overview</div>
          <select className="fgp-entity-select" value={activeEntity} onChange={e => setActiveEntity(e.target.value)}
            style={{ appearance: 'none', WebkitAppearance: 'none', background: `#FAFAF8 url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='8' height='5'%3E%3Cpath d='M0 0l4 5 4-5z' fill='%238C8C8C'/%3E%3C/svg%3E") no-repeat right 10px center`, border: '1px solid #E8E5E0', padding: '5px 28px 5px 12px', fontSize: 12, color: '#1A1A1A', cursor: 'pointer', outline: 'none', letterSpacing: 0.3 }}>
            <option value="All">All Entities</option>
            {Object.entries(ENTITY_SHORT).map(([k,v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </div>
        <div className="fgp-stat-line" style={{ display: 'flex', flexWrap: 'wrap', gap: '6px 20px', fontSize: 12, color: '#8C8C8C', marginBottom: 24, lineHeight: 1.8 }}>
          <span><strong style={{ color: '#1A1A1A', fontWeight: 600 }}>{data.length}</strong> total forms</span>
          <span style={{ color: '#E8E5E0' }}>·</span>
          <span><strong style={{ color: '__COLOR_ACCENT__', fontWeight: 600 }}>{active.length}</strong> billable</span>
          <span style={{ color: '#E8E5E0' }}>·</span>
          <span><strong style={{ color: '#1A1A1A', fontWeight: 600 }}>{data.filter(d=>d.readonly).length}</strong> read-only excluded</span>
          <span style={{ color: '#E8E5E0' }}>·</span>
          <span><strong style={{ color: '#1A1A1A', fontWeight: 600 }}>{notaryForms.length}</strong> notary required</span>
          <span style={{ color: '#E8E5E0' }}>·</span>
          <span><strong style={{ color: '#1A1A1A', fontWeight: 600 }}>{active.filter(d=>d.tp==="Yes").length}</strong> 3rd-party signer</span>
          <span style={{ color: '#E8E5E0' }}>·</span>
          <span><strong style={{ color: '#1A1A1A', fontWeight: 600 }}>{witnessForms.length}</strong> witness required</span>
        </div>

        <div style={{ borderLeft: '3px solid __COLOR_PRIMARY__', paddingLeft: 16, marginBottom: 32 }}>
          <div style={{ fontSize: 10, fontWeight: 600, color: '__COLOR_PRIMARY__', letterSpacing: 1, textTransform: 'uppercase', marginBottom: 4 }}>Insight</div>
          <div style={{ fontSize: 13, color: '#4A4A4A', lineHeight: 1.6 }}>
            {active.length} forms across {entityBreakdown.length} entities generate an estimated <strong style={{color:'__COLOR_ACCENT__'}}>{fmt(totalCost)}</strong> in digitization costs.
            The average form scores {Math.round(active.reduce((s,d)=>s+cxScore(d.c),0)/active.length)} out of 100 on complexity and has a {Math.round(active.reduce((s,d)=>s+d.ni,0)/active.length)}% NIGO rate.
          </div>
        </div>

        <div style={{ marginBottom: 32 }}>
          <div style={{ fontSize: 10, fontWeight: 600, color: '#8C8C8C', letterSpacing: 1, textTransform: 'uppercase', marginBottom: 12 }}>Cost by Entity</div>
          {entityBreakdown.map(eb => (
            <div key={eb.name} style={{ marginBottom: 10 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 3 }}>
                <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
                  <span style={{ fontSize: 13, fontWeight: 500, color: '#1A1A1A' }}>{eb.short}</span>
                  <span style={{ fontSize: 10, color: '#8C8C8C' }}>{eb.active} forms · {Math.round(eb.cost/totalCost*100)}%</span>
                </div>
                <span style={{ fontSize: 13, fontWeight: 600, color: '__COLOR_ACCENT__' }}>{fmt(eb.cost)}</span>
              </div>
              <div style={{ height: 3, background: '#F0EDE8', borderRadius: 1, overflow: 'hidden' }}>
                <div style={{ height: '100%', borderRadius: 1, width: `${(eb.cost / totalCost) * 100}%`, backgroundColor: ENTITY_COLORS[eb.name], transition: 'width 0.5s' }} />
              </div>
            </div>
          ))}
        </div>

        <div style={{ fontSize: 10, fontWeight: 600, color: '#8C8C8C', letterSpacing: 1, textTransform: 'uppercase', marginBottom: 8 }}>Complexity Quadrants</div>
        <div className="fgp-quadrants" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: 12, marginBottom: 8 }}>
          {[
            { label: 'Danger Zone', data: q1, color: '#9B2335', desc: 'High complexity + high NIGO' },
            { label: 'Complex but Accurate', data: q2, color: '#B8860B', desc: 'High complexity, low NIGO' },
            { label: 'Error-Prone', data: q3, color: '#C87533', desc: 'Low complexity, high NIGO' },
            { label: 'Quick Wins', data: q4, color: '__COLOR_ACCENT__', desc: 'Low complexity, low NIGO' },
          ].map(q => (
            <div key={q.label} style={{ padding: '12px 16px', borderLeft: `3px solid ${q.color}`, background: '#FFFFFF' }}>
              <div style={{ fontSize: 10, color: '#8C8C8C', letterSpacing: 0.3 }}>{q.label}</div>
              <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginTop: 2 }}>
                <span style={{ fontSize: 20, fontWeight: 300, color: q.color }}>{q.data.length}</span>
                <span style={{ fontSize: 11, color: '#8C8C8C' }}>{fmt(q.data.reduce((s,d)=>s+d.cost,0))}</span>
              </div>
            </div>
          ))}
        </div>
      </div>
    )}

    {/* HARDEST TAB */}
    {view === "hardest" && (
      <div className="fgp-content" style={{ maxWidth: 1200, margin: '0 auto', padding: '24px 24px' }}>
        <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 16 }}>
          <select className="fgp-entity-select" value={activeEntity} onChange={e => setActiveEntity(e.target.value)}
            style={{ appearance: 'none', WebkitAppearance: 'none', background: `#FAFAF8 url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='8' height='5'%3E%3Cpath d='M0 0l4 5 4-5z' fill='%238C8C8C'/%3E%3C/svg%3E") no-repeat right 10px center`, border: '1px solid #E8E5E0', padding: '5px 28px 5px 12px', fontSize: 12, color: '#1A1A1A', cursor: 'pointer', outline: 'none', letterSpacing: 0.3 }}>
            <option value="All">All Entities</option>
            {Object.entries(ENTITY_SHORT).map(([k,v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </div>
        <div style={{ borderLeft: '3px solid #9B2335', paddingLeft: 16, marginBottom: 24 }}>
          <div style={{ fontSize: 10, fontWeight: 600, color: '#9B2335', letterSpacing: 1, textTransform: 'uppercase', marginBottom: 4 }}>Where the Money Goes</div>
          <div style={{ fontSize: 13, color: '#4A4A4A', lineHeight: 1.6 }}>
            The top 12 forms by estimated digitization cost represent <strong style={{color:'__COLOR_ACCENT__'}}>{fmt(hardest.reduce((s,d)=>s+d.cost,0))}</strong> — {Math.round(hardest.reduce((s,d)=>s+d.cost,0)/totalCost*100)}% of the total portfolio.
          </div>
        </div>
        <div className="fgp-table-wrap" style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', fontSize: 12, borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ borderBottom: '1px solid #E8E5E0' }}>
                <th style={{ textAlign: 'left', padding: '8px 12px', fontSize: 10, fontWeight: 600, color: '#8C8C8C', letterSpacing: 0.5, textTransform: 'uppercase' }}>Form</th>
                <th className="fgp-hide-mobile" style={{ textAlign: 'left', padding: '8px 12px', fontSize: 10, fontWeight: 600, color: '#8C8C8C', letterSpacing: 0.5, textTransform: 'uppercase' }}>Entity</th>
                <th style={{ textAlign: 'center', padding: '8px 12px', fontSize: 10, fontWeight: 600, color: '#8C8C8C', letterSpacing: 0.5, textTransform: 'uppercase' }}>Complexity</th>
                <th className="fgp-hide-mobile" style={{ textAlign: 'center', padding: '8px 12px', fontSize: 10, fontWeight: 600, color: '#8C8C8C', letterSpacing: 0.5, textTransform: 'uppercase' }}>NIGO</th>
                <th className="fgp-hide-mobile" style={{ textAlign: 'center', padding: '8px 12px', fontSize: 10, fontWeight: 600, color: '#8C8C8C', letterSpacing: 0.5, textTransform: 'uppercase' }}>Sigs</th>
                <th className="fgp-hide-mobile" style={{ textAlign: 'center', padding: '8px 12px', fontSize: 10, fontWeight: 600, color: '#8C8C8C', letterSpacing: 0.5, textTransform: 'uppercase' }}>Flags</th>
                <th style={{ textAlign: 'right', padding: '8px 12px', fontSize: 10, fontWeight: 600, color: '#8C8C8C', letterSpacing: 0.5, textTransform: 'uppercase' }}>Est. Cost</th>
              </tr>
            </thead>
            <tbody>
              {hardest.map((d, i) => (
                <tr key={i} style={{ borderBottom: '1px solid #F0EDE8', transition: 'background 0.15s' }} onMouseEnter={e => e.currentTarget.style.background='#FAFAF8'} onMouseLeave={e => e.currentTarget.style.background='transparent'}>
                  <td style={{ padding: '10px 12px' }}><div onClick={() => setSelectedForm(d)} style={{ fontWeight: 500, color: '__COLOR_NAV__', maxWidth: 280, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', cursor: 'pointer' }} title={d.n}>{d.n}</div><div style={{ fontSize: 10, color: '#8C8C8C', marginTop: 1 }}>{d.p} pages · {d.f} fields</div></td>
                  <td className="fgp-hide-mobile" style={{ padding: '10px 12px' }}><span style={{ fontSize: 10, padding: '2px 8px', background: '#F0EDE8', color: '#4A4A4A', letterSpacing: 0.5 }}>{ENTITY_SHORT[d.e]}</span></td>
                  <td style={{ textAlign: 'center', padding: '10px 12px', fontFamily: 'monospace', fontWeight: 600, color: cxScore(d.c) > 70 ? '#9B2335' : cxScore(d.c) > 40 ? '#B8860B' : '#4A4A4A' }}>{cxScore(d.c)}</td>
                  <td className="fgp-hide-mobile" style={{ textAlign: 'center', padding: '10px 12px', fontFamily: 'monospace', fontWeight: 600, color: d.ni > __NIGO_HIGH__ ? '#9B2335' : d.ni > __NIGO_MED__ ? '#B8860B' : '#4A4A4A' }}>{d.ni}</td>
                  <td className="fgp-hide-mobile" style={{ textAlign: 'center', padding: '10px 12px', fontFamily: 'monospace' }}>{d.sc}</td>
                  <td className="fgp-hide-mobile" style={{ padding: '10px 12px' }}><div style={{ display: 'flex', gap: 4, justifyContent: 'center', flexWrap: 'wrap' }}>
                    {d.not === "Yes" && <span style={{ fontSize: 9, color: '#9B2335' }}>NOT</span>}
                    {d.tp === "Yes" && <span style={{ fontSize: 9, color: '#8C8C8C' }}>3P</span>}
                    {d.wit === "Yes" && <span style={{ fontSize: 9, color: '#B8860B' }}>WIT</span>}
                    {d.dl === "Yes" && <span style={{ fontSize: 9, color: '#5B7FB5' }}>DL</span>}
                  </div></td>
                  <td style={{ textAlign: 'right', padding: '10px 12px', fontWeight: 600, color: '__COLOR_ACCENT__' }}>{fmt(d.cost)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    )}

    {/* COORDINATION TAB */}
    {view === "coordination" && (
      <div className="fgp-content" style={{ maxWidth: 1200, margin: '0 auto', padding: '24px 24px' }}>
        <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 16 }}>
          <select className="fgp-entity-select" value={activeEntity} onChange={e => setActiveEntity(e.target.value)}
            style={{ appearance: 'none', WebkitAppearance: 'none', background: `#FAFAF8 url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='8' height='5'%3E%3Cpath d='M0 0l4 5 4-5z' fill='%238C8C8C'/%3E%3C/svg%3E") no-repeat right 10px center`, border: '1px solid #E8E5E0', padding: '5px 28px 5px 12px', fontSize: 12, color: '#1A1A1A', cursor: 'pointer', outline: 'none', letterSpacing: 0.3 }}>
            <option value="All">All Entities</option>
            {Object.entries(ENTITY_SHORT).map(([k,v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </div>
        <div style={{ borderLeft: '3px solid #B8860B', paddingLeft: 16, marginBottom: 24 }}>
          <div style={{ fontSize: 10, fontWeight: 600, color: '#B8860B', letterSpacing: 1, textTransform: 'uppercase', marginBottom: 4 }}>The Coordination Tax</div>
          <div style={{ fontSize: 13, color: '#4A4A4A', lineHeight: 1.6 }}>
            Beyond raw complexity, some forms impose a <strong>coordination burden</strong> — requiring notaries, witnesses, multiple signers, or third-party involvement.
            These forms cost more than their field counts suggest because every external dependency adds scheduling friction, error surface, and abandonment risk.
          </div>
        </div>

        <div className="fgp-stat-line" style={{ display: 'flex', flexWrap: 'wrap', gap: '6px 20px', fontSize: 12, color: '#8C8C8C', marginBottom: 24, lineHeight: 1.8 }}>
          <span><strong style={{ color: '#B8860B', fontWeight: 600 }}>{notaryForms.length}</strong> notarization required · <span style={{ color: '#8C8C8C' }}>{fmt(notaryForms.reduce((s,d)=>s+d.cost,0))}</span></span>
          <span style={{ color: '#E8E5E0' }}>·</span>
          <span><strong style={{ color: '#B8860B', fontWeight: 600 }}>{witnessForms.length}</strong> witness required · <span style={{ color: '#8C8C8C' }}>{fmt(witnessForms.reduce((s,d)=>s+d.cost,0))}</span></span>
          <span style={{ color: '#E8E5E0' }}>·</span>
          <span><strong style={{ color: '#B8860B', fontWeight: 600 }}>{active.filter(d=>d.sc>=5).length}</strong> 5+ signatures · <span style={{ color: '#8C8C8C' }}>{fmt(active.filter(d=>d.sc>=5).reduce((s,d)=>s+d.cost,0))}</span></span>
        </div>

        <div style={{ fontSize: 10, fontWeight: 600, color: '#8C8C8C', letterSpacing: 1, textTransform: 'uppercase', marginBottom: 10 }}>Highest Coordination Burden</div>
        <div>
          {highCoord.slice(0, 15).map((d, i) => (
            <div className="fgp-coord-item" key={i} style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '10px 0', borderBottom: '1px solid #F0EDE8' }}>
              <div style={{ display: 'flex', gap: 2 }}>
                {[1,2,3,4,5].map(j => (
                  <div key={j} style={{ width: 3, height: 20, borderRadius: 1, background: j <= d.coord ? '#B8860B' : '#E8E5E0' }} />
                ))}
              </div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div onClick={() => setSelectedForm(d)} style={{ fontSize: 13, fontWeight: 500, color: '__COLOR_NAV__', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', cursor: 'pointer' }}>{d.n}</div>
                <div style={{ display: 'flex', gap: 10, marginTop: 2 }}>
                  {d.not === "Yes" && <span style={{ fontSize: 10, color: '#9B2335' }}>Notary</span>}
                  {d.tp === "Yes" && <span style={{ fontSize: 10, color: '#8C8C8C' }}>3rd Party</span>}
                  {d.wit === "Yes" && <span style={{ fontSize: 10, color: '#B8860B' }}>Witness</span>}
                  {d.sc >= 3 && <span style={{ fontSize: 10, color: '#5B7FB5' }}>{d.sc} sigs</span>}
                </div>
              </div>
              <div style={{ fontSize: 13, fontWeight: 600, color: '__COLOR_ACCENT__', whiteSpace: 'nowrap' }}>{fmt(d.cost)}</div>
            </div>
          ))}
        </div>
      </div>
    )}

    {/* CONDITIONALITY TAB */}
    {view === "conditionality" && (
      <div className="fgp-content" style={{ maxWidth: 1200, margin: '0 auto', padding: '24px 24px' }}>
        <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 16 }}>
          <select className="fgp-entity-select" value={activeEntity} onChange={e => setActiveEntity(e.target.value)}
            style={{ appearance: 'none', WebkitAppearance: 'none', background: `#FAFAF8 url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='8' height='5'%3E%3Cpath d='M0 0l4 5 4-5z' fill='%238C8C8C'/%3E%3C/svg%3E") no-repeat right 10px center`, border: '1px solid #E8E5E0', padding: '5px 28px 5px 12px', fontSize: 12, color: '#1A1A1A', cursor: 'pointer', outline: 'none', letterSpacing: 0.3 }}>
            <option value="All">All Entities</option>
            {Object.entries(ENTITY_SHORT).map(([k,v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </div>
        <div style={{ borderLeft: '3px solid #5B7FB5', paddingLeft: 16, marginBottom: 24 }}>
          <div style={{ fontSize: 10, fontWeight: 600, color: '#5B7FB5', letterSpacing: 1, textTransform: 'uppercase', marginBottom: 4 }}>Hidden Dependencies</div>
          <div style={{ fontSize: 13, color: '#4A4A4A', lineHeight: 1.6 }}>
            <strong>{active.filter(d=>d.con==="Yes").length} of {active.length} billable forms</strong> are conditional — completing one may trigger or require another. This creates invisible cost multipliers: a customer doesn't just fill out one form, they enter a <strong>form chain</strong>. The true digitization ROI depends on mapping and collapsing these chains, not just converting individual PDFs.
          </div>
        </div>

        <div className="fgp-stat-line" style={{ display: 'flex', flexWrap: 'wrap', gap: '6px 20px', fontSize: 12, color: '#8C8C8C', marginBottom: 24, lineHeight: 1.8 }}>
          <span><strong style={{ color: '#5B7FB5', fontWeight: 600 }}>{Math.round(active.filter(d=>d.con==="Yes").length/active.length*100)}%</strong> conditional</span>
          <span style={{ color: '#E8E5E0' }}>·</span>
          <span><strong style={{ color: '#5B7FB5', fontWeight: 600 }}>{active.filter(d=>d.att==="Yes").length}</strong> require attachments</span>
          <span style={{ color: '#E8E5E0' }}>·</span>
          <span><strong style={{ color: '#5B7FB5', fontWeight: 600 }}>{active.filter(d=>d.id==="Yes").length}</strong> require ID verification</span>
        </div>

        <div style={{ fontSize: 10, fontWeight: 600, color: '#8C8C8C', letterSpacing: 1, textTransform: 'uppercase', marginBottom: 10 }}>Costliest Conditional Forms</div>
        <div className="fgp-table-wrap" style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', fontSize: 12, borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ borderBottom: '1px solid #E8E5E0' }}>
                <th style={{ textAlign: 'left', padding: '8px 12px', fontSize: 10, fontWeight: 600, color: '#8C8C8C', letterSpacing: 0.5, textTransform: 'uppercase' }}>Form</th>
                <th className="fgp-hide-mobile" style={{ textAlign: 'left', padding: '8px 12px', fontSize: 10, fontWeight: 600, color: '#8C8C8C', letterSpacing: 0.5, textTransform: 'uppercase' }}>Entity</th>
                <th style={{ textAlign: 'center', padding: '8px 12px', fontSize: 10, fontWeight: 600, color: '#8C8C8C', letterSpacing: 0.5, textTransform: 'uppercase' }}>Dependencies</th>
                <th style={{ textAlign: 'center', padding: '8px 12px', fontSize: 10, fontWeight: 600, color: '#8C8C8C', letterSpacing: 0.5, textTransform: 'uppercase' }}>Attach.</th>
                <th style={{ textAlign: 'right', padding: '8px 12px', fontSize: 10, fontWeight: 600, color: '#8C8C8C', letterSpacing: 0.5, textTransform: 'uppercase' }}>Est. Cost</th>
              </tr>
            </thead>
            <tbody>
              {conditional.map((d, i) => (
                <tr key={i} style={{ borderBottom: '1px solid #F0EDE8', transition: 'background 0.15s' }} onMouseEnter={e => e.currentTarget.style.background='#FAFAF8'} onMouseLeave={e => e.currentTarget.style.background='transparent'}>
                  <td style={{ padding: '10px 12px' }}><div onClick={() => setSelectedForm(d)} style={{ fontWeight: 500, color: '__COLOR_NAV__', maxWidth: 320, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', cursor: 'pointer' }}>{d.n}</div></td>
                  <td className="fgp-hide-mobile" style={{ padding: '10px 12px' }}><span style={{ fontSize: 10, padding: '2px 8px', background: '#F0EDE8', color: '#4A4A4A', letterSpacing: 0.5 }}>{ENTITY_SHORT[d.e]}</span></td>
                  <td style={{ padding: '10px 12px' }}><div style={{ display: 'flex', gap: 4, justifyContent: 'center', flexWrap: 'wrap' }}>
                    {d.con === "Yes" && <span style={{ fontSize: 9, padding: '1px 6px', background: '#F0EDE8', color: '#5B7FB5' }}>COND</span>}
                    {d.tp === "Yes" && <span style={{ fontSize: 9, padding: '1px 6px', background: '#F0EDE8', color: '#8C8C8C' }}>3P</span>}
                    {d.id === "Yes" && <span style={{ fontSize: 9, padding: '1px 6px', background: '#F0EDE8', color: '#B8860B' }}>ID</span>}
                  </div></td>
                  <td style={{ textAlign: 'center', padding: '10px 12px', fontSize: 12, color: '#4A4A4A' }}>{d.att === "Yes" ? `${d.ac} req'd` : "—"}</td>
                  <td style={{ textAlign: 'right', padding: '10px 12px', fontWeight: 600, color: '__COLOR_ACCENT__' }}>{fmt(d.cost)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    )}

    {/* CALCULATIONS TAB */}
    {view === "calculations" && (
      <div className="fgp-content" style={{ maxWidth: 1200, margin: '0 auto', padding: '24px 24px' }}>
        <div style={{ display: 'flex', gap: 32, flexWrap: 'wrap', marginBottom: 40 }}>
          <div style={{ flex: '1 1 340px' }}>
            <div style={{ fontSize: 14, fontWeight: 600, color: '#1A1A1A', marginBottom: 6 }}>Complexity Score</div>
            <div style={{ fontSize: 12, color: '#4A4A4A', lineHeight: 1.7, marginBottom: 16 }}>
              Measures how structurally demanding a form is to digitize. Scored 0–100 based on the total volume of content, instructions, and logic in the form.
            </div>
            <div style={{ fontSize: 10, fontWeight: 600, color: '#8C8C8C', letterSpacing: 1, textTransform: 'uppercase', marginBottom: 10 }}>What drives it up</div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {[
                { icon: '📄', label: 'Page count', desc: 'More pages = more content to convert' },
                { icon: '🔤', label: 'Number of fields', desc: 'Input fields, checkboxes, dropdowns' },
                { icon: '✍️', label: 'Signature count', desc: 'Each signer adds routing complexity' },
                { icon: '📎', label: 'Attachments required', desc: 'Supporting documents that must accompany the form' },
                { icon: '🔀', label: 'Conditional logic', desc: 'If/then branching based on selections' },
                { icon: '🆔', label: 'ID verification', desc: 'Identity validation steps' },
                { icon: '💰', label: 'Payment processing', desc: 'Payment fields or financial transactions' },
                { icon: '⏰', label: 'Deadlines', desc: 'Time-sensitive submission requirements' },
              ].map(item => (
                <div key={item.label} style={{ display: 'flex', alignItems: 'flex-start', gap: 8, fontSize: 12, color: '#4A4A4A', lineHeight: 1.5 }}>
                  <span style={{ fontSize: 11, flexShrink: 0, width: 18, textAlign: 'center' }}>{item.icon}</span>
                  <div><strong style={{ color: '#1A1A1A' }}>{item.label}</strong> — {item.desc}</div>
                </div>
              ))}
            </div>
          </div>
          <div style={{ flex: '1 1 340px' }}>
            <div style={{ fontSize: 14, fontWeight: 600, color: '#1A1A1A', marginBottom: 6 }}>NIGO Rate</div>
            <div style={{ fontSize: 12, color: '#4A4A4A', lineHeight: 1.7, marginBottom: 16 }}>
              <strong>Not In Good Order</strong> — the percentage of submissions returned due to errors. A NIGO rate of 80% means 4 out of 5 submissions have problems.
            </div>
            <div style={{ fontSize: 10, fontWeight: 600, color: '#8C8C8C', letterSpacing: 1, textTransform: 'uppercase', marginBottom: 10 }}>Common reasons for rejection</div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {[
                { icon: '🚫', label: 'Missing fields', desc: 'Required fields left blank' },
                { icon: '❌', label: 'Invalid signatures', desc: 'Wrong signer, missing initials, undated' },
                { icon: '📋', label: 'Incomplete attachments', desc: 'Required documents not included' },
                { icon: '⚠️', label: 'Incorrect information', desc: 'Mismatched names, account numbers, SSNs' },
                { icon: '📆', label: 'Expired or stale', desc: 'Submission past the deadline or outdated form version' },
                { icon: '🔏', label: 'Notary issues', desc: 'Missing or improperly executed notarization' },
                { icon: '👥', label: '3rd-party failures', desc: 'External signer did not complete their section' },
              ].map(item => (
                <div key={item.label} style={{ display: 'flex', alignItems: 'flex-start', gap: 8, fontSize: 12, color: '#4A4A4A', lineHeight: 1.5 }}>
                  <span style={{ fontSize: 11, flexShrink: 0, width: 18, textAlign: 'center' }}>{item.icon}</span>
                  <div><strong style={{ color: '#1A1A1A' }}>{item.label}</strong> — {item.desc}</div>
                </div>
              ))}
            </div>
          </div>
        </div>
        <div style={{ borderTop: '1px solid #E8E5E0', marginBottom: 32 }}></div>
        <div style={{ marginBottom: 12 }}>
          <div style={{ fontSize: 14, fontWeight: 600, color: '#1A1A1A', marginBottom: 6 }}>Docusign Web Forms Builder</div>
          <div style={{ fontSize: 12, color: '#4A4A4A', lineHeight: 1.7, marginBottom: 16 }}>
            See how Docusign IAM's AI Assisted Web Form Creation tool can automatically create guided digital experiences — saving you...millions.
          </div>
          <div style={{ position: 'relative', paddingBottom: '56.25%', height: 0, overflow: 'hidden', borderRadius: 8, background: '#000', boxShadow: '0 4px 24px rgba(0,0,0,0.12)', width: '100%', maxWidth: 920 }}>
            <iframe
              src="https://www.youtube.com/embed/2Rpp0gBK8Uo?rel=0"
              title="Docusign eSignature: How to Create a Web Form"
              frameBorder="0"
              allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
              allowFullScreen
              style={{ position: 'absolute', top: 0, left: 0, width: '100%', height: '100%' }}
            />
          </div>
          <div style={{ fontSize: 11, color: '#8C8C8C', marginTop: 8, fontStyle: 'italic' }}>
            Docusign eSignature: How to Create a Web Form (official tutorial)
          </div>
        </div>
      </div>
    )}

    {/* FOOTER */}
    <div className="fgp-footer" style={{ borderTop: '1px solid #E8E5E0', padding: '12px 24px', marginTop: 32, display: 'flex', justifyContent: 'center' }}>
      <div style={{ maxWidth: 1200, margin: '0 auto', width: '100%', textAlign: 'center', fontSize: 10, color: '#B0AAA0' }}>
        <span>DocuSign · Confidential</span>
      </div>
    </div>
    </div>
  );
}
