"use strict";

// Limited local editor, never a replacement for MCP-2's JSON Schema validation.
const MCPSchema = (() => {
  const keys = new Set(["type", "properties", "required", "default", "description", "title", "enum", "items", "additionalProperties",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minLength", "maxLength", "pattern", "minItems", "maxItems",
    "$schema", "$id", "$comment", "examples"]);
  const own = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
  const put = (object, key, value) => Object.defineProperty(object, key, { value, enumerable: true, configurable: true, writable: true });
  function supported(schema, depth = 0, budget = { count: 0 }) {
    if (!schema || typeof schema !== "object" || Array.isArray(schema) || depth > 4 || ++budget.count > 60) return false;
    if (Object.keys(schema).some((key) => !keys.has(key))) return false;
    if (!["object", "array", "string", "number", "integer", "boolean"].includes(schema.type)) return false;
    if (schema.enum && (!Array.isArray(schema.enum) || !schema.enum.length || schema.enum.length > 100 || schema.enum.some((value) => value !== null && typeof value === "object"))) return false;
    if (schema.type === "object") {
      if (schema.additionalProperties !== undefined && typeof schema.additionalProperties !== "boolean") return false;
      if (schema.properties !== undefined && (!schema.properties || typeof schema.properties !== "object" || Array.isArray(schema.properties))) return false;
      if (schema.required && (!Array.isArray(schema.required) || schema.required.some((key) => !own(schema.properties || {}, key)))) return false;
      return Object.values(schema.properties || {}).every((child) => supported(child, depth + 1, budget));
    }
    if (schema.type === "array") return Boolean(schema.items && supported(schema.items, depth + 1, budget));
    return true;
  }
  function validate(schema, value, name = "Arguments") {
    const fail = (message) => { throw new Error(`${name}: ${message}`); };
    if (schema.enum && !schema.enum.some((entry) => JSON.stringify(entry) === JSON.stringify(value))) fail("select an enum value");
    if (schema.type === "object") {
      if (!value || typeof value !== "object" || Array.isArray(value)) fail("must be an object");
      for (const key of schema.required || []) if (!own(value, key)) fail(`missing required field ${key}`);
      for (const key of Object.keys(value)) {
        if (own(schema.properties || {}, key)) validate(schema.properties[key], value[key], `${name}.${key}`);
        else if (schema.additionalProperties === false) fail(`unknown field ${key}`);
      }
    } else if (schema.type === "array") {
      if (!Array.isArray(value)) fail("must be an array");
      if (schema.minItems !== undefined && value.length < schema.minItems) fail("too few items");
      if (schema.maxItems !== undefined && value.length > schema.maxItems) fail("too many items");
      value.forEach((item, index) => validate(schema.items, item, `${name}[${index}]`));
    } else if (schema.type === "string") {
      if (typeof value !== "string") fail("must be text");
      const length = Array.from(value).length;
      if (schema.minLength !== undefined && length < schema.minLength) fail("text is too short");
      if (schema.maxLength !== undefined && length > schema.maxLength) fail("text is too long");
      // Patterns are evaluated by the server, avoiding untrusted regex execution here.
    } else if (schema.type === "boolean") {
      if (typeof value !== "boolean") fail("must be true or false");
    } else {
      if (typeof value !== "number" || !Number.isFinite(value)) fail("must be a finite number");
      if (schema.type === "integer" && !Number.isSafeInteger(value)) fail("must be a safe integer");
      if (schema.minimum !== undefined && value < schema.minimum) fail("below minimum");
      if (schema.maximum !== undefined && value > schema.maximum) fail("above maximum");
      if (schema.exclusiveMinimum !== undefined && value <= schema.exclusiveMinimum) fail("below exclusive minimum");
      if (schema.exclusiveMaximum !== undefined && value >= schema.exclusiveMaximum) fail("above exclusive maximum");
    }
    return value;
  }
  function example(schema, depth = 0) {
    if (!schema || depth > 4) return {};
    if (own(schema, "default")) return schema.default;
    if (Array.isArray(schema.enum) && schema.enum.length) return schema.enum[0];
    if (schema.type === "object") {
      const value = {};
      for (const [key, child] of Object.entries(schema.properties || {})) if ((schema.required || []).includes(key) || own(child, "default")) put(value, key, example(child, depth + 1));
      return value;
    }
    if (schema.type === "array") return [];
    if (schema.type === "boolean") return false;
    if (schema.type === "number" || schema.type === "integer") return schema.minimum || 0;
    return "";
  }
  function render(document, container, schema) {
    container.replaceChildren();
    if (schema?.type !== "object" || !supported(schema)) return { supported: false, setDisabled() {} };
    const controls = [];
    let editingDisabled = false;
    const make = (tag, text = "") => { const element = document.createElement(tag); element.textContent = text; return element; };
    let sequence = 0;
    function objectFields(parent, current, preset) {
      const readers = [];
      for (const [name, field] of Object.entries(current.properties || {})) {
        const required = (current.required || []).includes(name);
        const hasPreset = preset && own(preset, name);
        const hasDefault = own(field, "default");
        const value = hasPreset ? preset[name] : hasDefault ? field.default : undefined;
        const group = make("fieldset"), legend = make("legend", `${name}${required ? " *" : " (optional)"}`);
        group.append(legend);
        const include = make("input"); include.type = "checkbox"; include.checked = required || hasPreset || hasDefault;
        const includeLabel = make("label", "Include this field "); includeLabel.append(include); group.append(includeLabel);
        if (field.description) group.append(make("small", field.description));
        let control, read;
        if (field.enum) {
          control = make("select"); const placeholder = make("option", "Select a value"); placeholder.value = ""; control.append(placeholder); control.value = "";
          field.enum.forEach((entry, index) => { const option = make("option", JSON.stringify(entry)); option.value = String(index); control.append(option); });
          if (value !== undefined) control.value = String(field.enum.findIndex((entry) => JSON.stringify(entry) === JSON.stringify(value)));
          read = () => { if (!/^\d+$/.test(control.value) || !own(field.enum, control.value)) throw new Error(`${name}: select a value`); return field.enum[Number(control.value)]; };
        } else if (field.type === "object") {
          control = make("div"); const nested = objectFields(control, field, value);
          read = () => nested();
        } else if (field.type === "array") {
          control = make("textarea"); control.rows = 3; control.value = JSON.stringify(value === undefined ? [] : value, null, 2);
          read = () => { try { return JSON.parse(control.value); } catch (_) { throw new Error(`${name}: enter a JSON array`); } };
        } else {
          control = make("input"); control.type = field.type === "boolean" ? "checkbox" : ["integer", "number"].includes(field.type) ? "number" : "text";
          if (control.type === "checkbox") control.checked = value === undefined ? false : value;
          else control.value = value === undefined ? "" : String(value);
          if (control.type === "number") control.step = field.type === "integer" ? "1" : "any";
          read = () => control.type === "checkbox" ? control.checked : control.type === "number" ? (control.value.trim() === "" ? NaN : Number(control.value)) : control.value;
        }
        control.id = `mcp-field-${++sequence}`;
        control.setAttribute("aria-label", name);
        group.append(control); parent.append(group);
        controls.push({ include, control, required, group });
        include.addEventListener("change", () => setDisabled(editingDisabled));
        readers.push(() => {
          if (!include.checked) { if (required) throw new Error(`${name}: required field is missing`); return null; }
          return { name, value: validate(field, read(), name) };
        });
      }
      return () => { const result = {}; for (const read of readers) { const entry = read(); if (entry) put(result, entry.name, entry.value); } return result; };
    }
    const read = objectFields(container, schema, schema.default);
    const setDisabled = (disabled) => {
      editingDisabled = disabled;
      for (const entry of controls) {
        entry.include.disabled = disabled || entry.required;
        entry.control.disabled = disabled || !entry.include.checked;
        // Fieldset disabling also covers nested controls when the object is omitted.
        entry.group.disabled = disabled;
        if (entry.control.tagName === "DIV") for (const child of entry.control.querySelectorAll("fieldset")) child.disabled = disabled || !entry.include.checked;
      }
    };
    setDisabled(false);
    return { supported: true, read: () => validate(schema, read()), setDisabled };
  }
  return { render, supported, validate, example };
})();
if (typeof module !== "undefined" && module.exports) module.exports = MCPSchema;
if (typeof window !== "undefined") window.MCPSchema = MCPSchema;
