import { useId } from 'react';
import { presetById, type SettingsModel } from '../../api/settings.ts';
import type { EditableSettings } from '../../api/types.ts';
import { TechBlock } from '../../components/Tech.tsx';
import {
  areaShortName,
  fmtCount,
  fmtPercent,
  fmtPresetLabel,
  fmtRange,
  fmtSpeed,
  strategyName,
} from '../../lib/format.ts';
import { SettingsEditor } from './SettingsEditor.tsx';

interface Props {
  model: SettingsModel;
  draft: EditableSettings;
  onDraft: (next: EditableSettings) => void;
  editing: boolean;
  onEditing: (editing: boolean) => void;
}

/** What will run, in plain words, placed above the Start button (docs/UX.md, Start screen). */
export function RunSettingsCard({ model, draft, onDraft, editing, onEditing }: Props) {
  const headingId = useId();
  const editorId = useId();
  const d = model.defaults;
  const preset = presetById(model.presets, draft.preset);
  const strategy = model.strategies.find((s) => s.value === draft.strategy);
  const areas = Object.keys(d.locations ?? {});

  return (
    <section className="panel run-card" aria-labelledby={headingId}>
      <div className="panel-head">
        <h2 className="panel-title" id={headingId}>
          Run settings
        </h2>
        <div className="panel-actions">
          <button
            type="button"
            className="linklike"
            aria-expanded={editing}
            aria-controls={editorId}
            onClick={() => onEditing(!editing)}
          >
            {editing ? 'Hide settings' : 'Change settings'}
          </button>
        </div>
      </div>
      <p className="panel-what">
        <span className="sr-only">What is this? </span>
        This is what the simulation will do when you press Start.
      </p>

      <dl className="settings-list">
        <dt>Batteries</dt>
        <dd>{fmtCount(d.fleet_size)} home batteries</dd>

        <dt>Each battery</dt>
        <dd>
          {fmtRange(d.power_kw_range[0], d.power_kw_range[1])} kW,{' '}
          {fmtRange(d.capacity_kwh_range[0], d.capacity_kwh_range[1])} kWh each{' '}
          <span className="muted">(how fast it charges, and how much it holds)</span>
        </dd>

        <dt>Where</dt>
        <dd>{areas.length ? areas.map(areaShortName).join(' & ') : 'Houston & Dallas–Fort Worth'}</dd>

        <dt>Replay</dt>
        <dd>
          {preset ? fmtPresetLabel(preset) : draft.preset}
          {preset?.description ? <div className="muted small">{preset.description}</div> : null}
        </dd>

        <dt>Speed</dt>
        <dd>{fmtSpeed(draft.speed_tps)}</dd>

        <dt>Decision method</dt>
        <dd>{strategy?.label ?? strategyName(draft.strategy)}</dd>

        <dt>Starting charge</dt>
        <dd>Batteries start {fmtPercent(draft.start_charge_level)} charged</dd>
      </dl>

      <TechBlock
        rows={[
          ['preset', draft.preset],
          ['start_utc', preset?.start_utc ?? d.start_utc],
          ['speed_tps', `${draft.speed_tps} (5-minute steps per second)`],
          ['strategy', draft.strategy],
          ['start_charge_level', String(draft.start_charge_level)],
          ['fleet_size', String(d.fleet_size)],
          ['locations', JSON.stringify(d.locations)],
          ['editable', model.editable.join(', ')],
        ]}
      >
        {model.fallback ? <div>GET /settings failed; showing the documented defaults.</div> : null}
      </TechBlock>

      <div id={editorId} hidden={!editing}>
        {editing ? <SettingsEditor model={model} draft={draft} onDraft={onDraft} onDone={() => onEditing(false)} /> : null}
      </div>
    </section>
  );
}
