import { useId } from 'react';
import type { SettingsModel } from '../../api/settings.ts';
import type { EditableSettings } from '../../api/types.ts';
import { fmtLocalDate, fmtPercent, fmtTimeOfDay, localDateOf } from '../../lib/format.ts';

interface Props {
  model: SettingsModel;
  draft: EditableSettings;
  onDraft: (next: EditableSettings) => void;
  onDone: () => void;
}

/** Only preset, speed, decision method and starting charge can be changed (docs/API.md). */
export function SettingsEditor({ model, draft, onDraft, onDone }: Props) {
  const ids = { speed: useId(), charge: useId(), chargeHint: useId() };
  const can = (key: keyof EditableSettings) => model.editable.includes(key);
  const set = <K extends keyof EditableSettings>(key: K, value: EditableSettings[K]) => onDraft({ ...draft, [key]: value });
  const backToDefaults = () =>
    onDraft({
      preset: model.defaults.preset,
      speed_tps: model.defaults.speed_tps,
      strategy: model.defaults.strategy,
      start_charge_level: model.defaults.start_charge_level,
    });

  return (
    <div className="editor">
      {can('preset') ? (
        <fieldset>
          <legend>Replay</legend>
          <div className="choice-list">
            {model.presets.map((p) => {
              const date = p.date_local ?? (p.start_utc ? localDateOf(p.start_utc) : null);
              return (
                <label key={p.id} className="choice">
                  <input
                    type="radio"
                    name="preset"
                    value={p.id}
                    checked={draft.preset === p.id}
                    onChange={() => set('preset', p.id)}
                  />
                  <span className="choice-text">
                    <span className="choice-title">{p.label}</span>
                    <span className="choice-desc">
                      {date ? fmtLocalDate(date) : null}
                      {p.start_utc ? `, from ${fmtTimeOfDay(p.start_utc, { compact: true })}` : null}
                      {p.description ? ` · ${p.description}` : null}
                    </span>
                  </span>
                </label>
              );
            })}
          </div>
        </fieldset>
      ) : null}

      {can('strategy') ? (
        <fieldset>
          <legend>Decision method</legend>
          <div className="choice-list">
            {model.strategies.map((s) => (
              <label key={s.value} className="choice">
                <input
                  type="radio"
                  name="strategy"
                  value={s.value}
                  checked={draft.strategy === s.value}
                  onChange={() => set('strategy', s.value)}
                />
                <span className="choice-text">
                  <span className="choice-title">{s.label}</span>
                  {s.description ? <span className="choice-desc">{s.description}</span> : null}
                </span>
              </label>
            ))}
          </div>
        </fieldset>
      ) : null}

      <div className="editor-row">
        {can('speed_tps') ? (
          <div className="field">
            <label className="field-label" htmlFor={ids.speed}>
              Speed
            </label>
            <select
              id={ids.speed}
              value={String(draft.speed_tps)}
              onChange={(e) => set('speed_tps', Number(e.target.value))}
            >
              {model.speeds.map((s) => (
                <option key={s.value} value={String(s.value)}>
                  {s.label}
                </option>
              ))}
            </select>
          </div>
        ) : null}

        {can('start_charge_level') ? (
          <div className="field">
            <label className="field-label" htmlFor={ids.charge}>
              Starting charge: {fmtPercent(draft.start_charge_level)}
            </label>
            <input
              id={ids.charge}
              type="range"
              min={20}
              max={100}
              step={5}
              value={Math.round(draft.start_charge_level * 100)}
              aria-valuetext={`${fmtPercent(draft.start_charge_level)} charged`}
              aria-describedby={ids.chargeHint}
              onChange={(e) => set('start_charge_level', Number(e.target.value) / 100)}
            />
            <span className="field-hint" id={ids.chargeHint}>
              Batteries never go below 20%: that is their safety reserve.
            </span>
          </div>
        ) : null}
      </div>

      <div className="editor-actions">
        <button type="button" className="btn btn-sm btn-primary" onClick={onDone}>
          Done
        </button>
        <button type="button" className="btn btn-sm btn-quiet" onClick={backToDefaults}>
          Back to defaults
        </button>
      </div>
    </div>
  );
}
