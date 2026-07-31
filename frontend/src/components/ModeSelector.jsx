import React from "react";

export default function ModeSelector({ modes, selected, onSelect }) {
  return (
    <div className="mode-grid">
      {modes.map((m) => (
        <div
          key={m.id}
          className={`mode-card ${selected === m.id ? "selected" : ""}`}
          onClick={() => onSelect(m.id)}
        >
          <div className="name">{m.name}</div>
          <div className="desc">{m.executor_type}</div>
        </div>
      ))}
    </div>
  );
}
