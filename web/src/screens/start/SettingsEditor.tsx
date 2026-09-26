import { useId } from 'react';
import { draftFrom, presetsForYear, withPreset, withYear, yearOption, type SettingsModel } from '../../api/settings.ts';
import type { EditableSettings } from '../../api/types.ts';
import { Segmented } from '../../components/Segmented.tsx';
import { fmtLocalDate, fmtPercent, fmtTimeOfDay, localDateOf } from '../../lib/format.ts';

interface Props {
  model: SettingsModel;
  draft: EditableSettings;
  onDraft: (next: EditableSettings) => void;
  onDone: () => void;
}

/**
 * Only the year, preset, speed, decision method and starting charge can be changed (docs/API.md). Picking a
 * year offers that year's presets.
 */
export function SettingsEditor({ model, draft, onDraft, onDone }: Props) {
  const ids = { speed: useId(), charge: useId(), chargeHint: useId(), yearNote: useId() };
  const can = (key: keyof EditableSettings) => model.editable.includes(key);
  const set = <K extends keyof EditableSettings>(key: K, value: EditableSettings[K]) => onDraft({ ...draft, [key]: value });
  const backToDefaults = () => onDraft(draftFrom(model));
  // The year picks which presets are offered, so it shows whenever there is more than one to choose from.
  const showYears = can('preset') && model.years.length > 1;
  const year = yearOption(model, draft.year);
  const presets = presetsForYear(model.presets, draft.year);

  return (
    <div className="editor">
      {showYears ? (
        <div className="editor-year">
          <Segmented<string>
            label="Year"
            value={String(draft.year)}
            onChange={(v) => onDraft(withYear(draft, Number(v), model))}
            options={model.years.map((y) => ({ value: String(y.year), label: y.label }))}
          />
          {year?.realUntilUtc && year.description ? (
            <p className="field-hint year-note" id={ids.yearNote}>
              {year.description}
            </p>
          ) : null}
        </div>
      ) : null}

      {can('preset') ? (
        <fieldset>
          <legend>Replay</legend>
          <div className="choice-list">
            {(presets.length ? presets : model.presets).map((p) => {
              const date = p.date_local ?? (p.start_utc ? localDateOf(p.start_utc) : null);
              return (
                <label key={p.id} className="choice">
                  <input
                    type="radio"
                    name="preset"
                    value={p.id}
                    checked={draft.preset === p.id}
                    onChange={() => onDraft(withPreset(draft, p.id, model))}
                  />
                  <span className="choice-text">
                    <span className="choice-title">{p.label}</span>
                    <span className="choice-desc">
                      {date ? fmtLocalDate(date) : null}
                      {p.start_utc ? `, from ${fmtTimeOfDay(p.start_utc, { compact: true, words: true })}` : null}
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
