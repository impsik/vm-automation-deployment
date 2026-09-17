"use strict";
/*
   Copyright (C) 2012 by Jeremy P. White <jwhite@codeweavers.com>

   This file is part of spice-html5.

   spice-html5 is free software: you can redistribute it and/or modify
   it under the terms of the GNU Lesser General Public License as published by
   the Free Software Foundation, either version 3 of the License, or
   (at your option) any later version.

   spice-html5 is distributed in the hope that it will be useful,
   but WITHOUT ANY WARRANTY; without even the implied warranty of
   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
   GNU Lesser General Public License for more details.

   You should have received a copy of the GNU Lesser General Public License
   along with spice-html5.  If not, see <http://www.gnu.org/licenses/>.
*/

import * as Messages from './spicemsg.js';
import { Constants } from './enums.js';
import { KeyNames } from './atKeynames.js';
import { SpiceConn } from './spiceconn.js';
import { DEBUG } from './utils.js';

/*----------------------------------------------------------------------------
 ** Modifier Keystates
 **     These need to be tracked because focus in and out can get the keyboard
 **     out of sync.
 **------------------------------------------------------------------------*/
var Shift_state = -1;
var Ctrl_state = -1;
var Alt_state = -1;
var Meta_state = -1;

/*----------------------------------------------------------------------------
**  SpiceInputsConn
**      Drive the Spice Inputs channel (e.g. mouse + keyboard)
**--------------------------------------------------------------------------*/
function SpiceInputsConn()
{
    SpiceConn.apply(this, arguments);

    this.mousex = undefined;
    this.mousey = undefined;
    this.button_state = 0;
    this.waiting_for_ack = 0;
}

SpiceInputsConn.prototype = Object.create(SpiceConn.prototype);
SpiceInputsConn.prototype.process_channel_message = function(msg)
{
    if (msg.type == Constants.SPICE_MSG_INPUTS_INIT)
    {
        var inputs_init = new Messages.SpiceMsgInputsInit(msg.data);
        this.keyboard_modifiers = inputs_init.keyboard_modifiers;
        DEBUG > 1 && console.log("MsgInputsInit - modifier " + this.keyboard_modifiers);
        // FIXME - We don't do anything with the keyboard modifiers...
        return true;
    }
    if (msg.type == Constants.SPICE_MSG_INPUTS_KEY_MODIFIERS)
    {
        var key = new Messages.SpiceMsgInputsKeyModifiers(msg.data);
        this.keyboard_modifiers = key.keyboard_modifiers;
        DEBUG > 1 && console.log("MsgInputsKeyModifiers - modifier " + this.keyboard_modifiers);
        // FIXME - We don't do anything with the keyboard modifiers...
        return true;
    }
    if (msg.type == Constants.SPICE_MSG_INPUTS_MOUSE_MOTION_ACK)
    {
        DEBUG > 1 && console.log("mouse motion ack");
        this.waiting_for_ack -= Constants.SPICE_INPUT_MOTION_ACK_BUNCH;
        return true;
    }
    return false;
}



function handle_mousemove(e)
{
    var msg = new Messages.SpiceMiniData();
    var move;
    if (this.sc.mouse_mode == Constants.SPICE_MOUSE_MODE_CLIENT)
    {
        move = new Messages.SpiceMsgcMousePosition(this.sc, e)
        msg.build_msg(Constants.SPICE_MSGC_INPUTS_MOUSE_POSITION, move);
    }
    else
    {
        move = new Messages.SpiceMsgcMouseMotion(this.sc, e)
        msg.build_msg(Constants.SPICE_MSGC_INPUTS_MOUSE_MOTION, move);
    }
    if (this.sc && this.sc.inputs && this.sc.inputs.state === "ready")
    {
        if (this.sc.inputs.waiting_for_ack < (2 * Constants.SPICE_INPUT_MOTION_ACK_BUNCH))
        {
            this.sc.inputs.send_msg(msg);
            this.sc.inputs.waiting_for_ack++;
        }
        else
        {
            DEBUG > 0 && this.sc.log_info("Discarding mouse motion");
        }
    }

    if (this.sc && this.sc.cursor && this.sc.cursor.spice_simulated_cursor)
    {
        this.sc.cursor.spice_simulated_cursor.style.display = 'block';
        this.sc.cursor.spice_simulated_cursor.style.left = e.pageX - this.sc.cursor.spice_simulated_cursor.spice_hot_x + 'px';
        this.sc.cursor.spice_simulated_cursor.style.top = e.pageY - this.sc.cursor.spice_simulated_cursor.spice_hot_y + 'px';
        e.preventDefault();
    }

}

function handle_mousedown(e)
{
    var press = new Messages.SpiceMsgcMousePress(this.sc, e)
    var msg = new Messages.SpiceMiniData();
    msg.build_msg(Constants.SPICE_MSGC_INPUTS_MOUSE_PRESS, press);
    if (this.sc && this.sc.inputs && this.sc.inputs.state === "ready")
        this.sc.inputs.send_msg(msg);

    e.preventDefault();
}

function handle_contextmenu(e)
{
    e.preventDefault();
    return false;
}

function handle_mouseup(e)
{
    var release = new Messages.SpiceMsgcMouseRelease(this.sc, e)
    var msg = new Messages.SpiceMiniData();
    msg.build_msg(Constants.SPICE_MSGC_INPUTS_MOUSE_RELEASE, release);
    if (this.sc && this.sc.inputs && this.sc.inputs.state === "ready")
        this.sc.inputs.send_msg(msg);

    e.preventDefault();
}

function handle_mousewheel(e)
{
    var press = new Messages.SpiceMsgcMousePress;
    var release = new Messages.SpiceMsgcMouseRelease;
    if (e.deltaY < 0)
        press.button = release.button = Constants.SPICE_MOUSE_BUTTON_UP;
    else
        press.button = release.button = Constants.SPICE_MOUSE_BUTTON_DOWN;
    press.buttons_state = 0;
    release.buttons_state = 0;

    var msg = new Messages.SpiceMiniData();
    msg.build_msg(Constants.SPICE_MSGC_INPUTS_MOUSE_PRESS, press);
    if (this.sc && this.sc.inputs && this.sc.inputs.state === "ready")
        this.sc.inputs.send_msg(msg);

    msg.build_msg(Constants.SPICE_MSGC_INPUTS_MOUSE_RELEASE, release);
    if (this.sc && this.sc.inputs && this.sc.inputs.state === "ready")
        this.sc.inputs.send_msg(msg);

    e.preventDefault();
}

function handle_keydown(e)
{
    var key = new Messages.SpiceMsgcKeyDown(e)
    var msg = new Messages.SpiceMiniData();
    check_and_update_modifiers(e, key.code, this.sc);
    msg.build_msg(Constants.SPICE_MSGC_INPUTS_KEY_DOWN, key);
    if (this.sc && this.sc.inputs && this.sc.inputs.state === "ready")
        this.sc.inputs.send_msg(msg);

    e.preventDefault();
}

function handle_keyup(e)
{
    var key = new Messages.SpiceMsgcKeyUp(e)
    var msg = new Messages.SpiceMiniData();
    check_and_update_modifiers(e, key.code, this.sc);
    msg.build_msg(Constants.SPICE_MSGC_INPUTS_KEY_UP, key);
    if (this.sc && this.sc.inputs && this.sc.inputs.state === "ready")
        this.sc.inputs.send_msg(msg);

    e.preventDefault();
}

function sendCtrlAltDel(sc)
{
    if (sc && sc.inputs && sc.inputs.state === "ready"){
        var key = new Messages.SpiceMsgcKeyDown();
        var msg = new Messages.SpiceMiniData();

        update_modifier(true, KeyNames.KEY_LCtrl, sc);
        update_modifier(true, KeyNames.KEY_Alt, sc);

        key.code = KeyNames.KEY_KP_Decimal;
        msg.build_msg(Constants.SPICE_MSGC_INPUTS_KEY_DOWN, key);
        sc.inputs.send_msg(msg);
        msg.build_msg(Constants.SPICE_MSGC_INPUTS_KEY_UP, key);
        sc.inputs.send_msg(msg);

        if(Ctrl_state == false) update_modifier(false, KeyNames.KEY_LCtrl, sc);
        if(Alt_state == false) update_modifier(false, KeyNames.KEY_Alt, sc);
    }
}

const TEXT_KEY_MAP = {
    "1": [KeyNames.KEY_1, false], "!": [KeyNames.KEY_1, true],
    "2": [KeyNames.KEY_2, false], "@": [KeyNames.KEY_2, true],
    "3": [KeyNames.KEY_3, false], "#": [KeyNames.KEY_3, true],
    "4": [KeyNames.KEY_4, false], "$": [KeyNames.KEY_4, true],
    "5": [KeyNames.KEY_5, false], "%": [KeyNames.KEY_5, true],
    "6": [KeyNames.KEY_6, false], "^": [KeyNames.KEY_6, true],
    "7": [KeyNames.KEY_7, false], "&": [KeyNames.KEY_7, true],
    "8": [KeyNames.KEY_8, false], "*": [KeyNames.KEY_8, true],
    "9": [KeyNames.KEY_9, false], "(": [KeyNames.KEY_9, true],
    "0": [KeyNames.KEY_0, false], ")": [KeyNames.KEY_0, true],
    "-": [KeyNames.KEY_Minus, false], "_": [KeyNames.KEY_Minus, true],
    "=": [KeyNames.KEY_Equal, false], "+": [KeyNames.KEY_Equal, true],
    "[": [KeyNames.KEY_LBrace, false], "{": [KeyNames.KEY_LBrace, true],
    "]": [KeyNames.KEY_RBrace, false], "}": [KeyNames.KEY_RBrace, true],
    ";": [KeyNames.KEY_SemiColon, false], ":": [KeyNames.KEY_SemiColon, true],
    "'": [KeyNames.KEY_Quote, false], "\"": [KeyNames.KEY_Quote, true],
    "`": [KeyNames.KEY_Tilde, false], "~": [KeyNames.KEY_Tilde, true],
    "\\": [KeyNames.KEY_BSlash, false], "|": [KeyNames.KEY_BSlash, true],
    ",": [KeyNames.KEY_Comma, false], "<": [KeyNames.KEY_Comma, true],
    ".": [KeyNames.KEY_Period, false], ">": [KeyNames.KEY_Period, true],
    "/": [KeyNames.KEY_Slash, false], "?": [KeyNames.KEY_Slash, true],
    " ": [KeyNames.KEY_Space, false],
    "\t": [KeyNames.KEY_Tab, false],
    "\n": [KeyNames.KEY_Enter, false],
};

for (const letter of "abcdefghijklmnopqrstuvwxyz")
{
    const code = KeyNames["KEY_" + letter.toUpperCase()];
    TEXT_KEY_MAP[letter] = [code, false];
    TEXT_KEY_MAP[letter.toUpperCase()] = [code, true];
}

function send_scan_code(sc, code, pressed)
{
    if (!sc || !sc.inputs || sc.inputs.state !== "ready")
        return false;
    var key = pressed ? new Messages.SpiceMsgcKeyDown() : new Messages.SpiceMsgcKeyUp();
    var msg = new Messages.SpiceMiniData();
    key.code = pressed ? code : (0x80 | code);
    msg.build_msg(
        pressed ? Constants.SPICE_MSGC_INPUTS_KEY_DOWN : Constants.SPICE_MSGC_INPUTS_KEY_UP,
        key
    );
    sc.inputs.send_msg(msg);
    return true;
}

async function sendText(sc, text)
{
    var unsupported = 0;
    var sent = 0;
    text = String(text).replace(/\r\n?/g, "\n").slice(0, 10000);
    send_scan_code(sc, KeyNames.KEY_LCtrl, false);
    send_scan_code(sc, KeyNames.KEY_ShiftL, false);
    send_scan_code(sc, KeyNames.KEY_Alt, false);
    await new Promise(resolve => setTimeout(resolve, 20));
    for (const character of text)
    {
        const mapping = TEXT_KEY_MAP[character];
        if (!mapping)
        {
            unsupported++;
            continue;
        }
        if (mapping[1])
            send_scan_code(sc, KeyNames.KEY_ShiftL, true);
        if (mapping[1])
            await new Promise(resolve => setTimeout(resolve, 3));
        send_scan_code(sc, mapping[0], true);
        await new Promise(resolve => setTimeout(resolve, 5));
        send_scan_code(sc, mapping[0], false);
        if (mapping[1])
        {
            await new Promise(resolve => setTimeout(resolve, 3));
            send_scan_code(sc, KeyNames.KEY_ShiftL, false);
        }
        sent++;
        await new Promise(resolve => setTimeout(
            resolve,
            character === "\n" ? 35 : (sent % 40 === 0 ? 25 : 7)
        ));
    }
    return unsupported;
}

function update_modifier(state, code, sc)
{
    var msg = new Messages.SpiceMiniData();
    if (!state)
    {
        var key = new Messages.SpiceMsgcKeyUp()
        key.code =(0x80|code);
        msg.build_msg(Constants.SPICE_MSGC_INPUTS_KEY_UP, key);
    }
    else
    {
        var key = new Messages.SpiceMsgcKeyDown()
        key.code = code;
        msg.build_msg(Constants.SPICE_MSGC_INPUTS_KEY_DOWN, key);
    }

    sc.inputs.send_msg(msg);
}

function check_and_update_modifiers(e, code, sc)
{
    if (Shift_state === -1)
    {
        Shift_state = e.shiftKey;
        Ctrl_state = e.ctrlKey;
        Alt_state = e.altKey;
        Meta_state = e.metaKey;
    }

    if (code === KeyNames.KEY_ShiftL)
        Shift_state = true;
    else if (code === KeyNames.KEY_Alt)
        Alt_state = true;
    else if (code === KeyNames.KEY_LCtrl)
        Ctrl_state = true;
    else if (code === 0xE0B5)
        Meta_state = true;
    else if (code === (0x80|KeyNames.KEY_ShiftL))
        Shift_state = false;
    else if (code === (0x80|KeyNames.KEY_Alt))
        Alt_state = false;
    else if (code === (0x80|KeyNames.KEY_LCtrl))
        Ctrl_state = false;
    else if (code === (0x80|0xE0B5))
        Meta_state = false;

    if (sc && sc.inputs && sc.inputs.state === "ready")
    {
        if (Shift_state != e.shiftKey)
        {
            console.log("Shift state out of sync");
            update_modifier(e.shiftKey, KeyNames.KEY_ShiftL, sc);
            Shift_state = e.shiftKey;
        }
        if (Alt_state != e.altKey)
        {
            console.log("Alt state out of sync");
            update_modifier(e.altKey, KeyNames.KEY_Alt, sc);
            Alt_state = e.altKey;
        }
        if (Ctrl_state != e.ctrlKey)
        {
            console.log("Ctrl state out of sync");
            update_modifier(e.ctrlKey, KeyNames.KEY_LCtrl, sc);
            Ctrl_state = e.ctrlKey;
        }
        if (Meta_state != e.metaKey)
        {
            console.log("Meta state out of sync");
            update_modifier(e.metaKey, 0xE0B5, sc);
            Meta_state = e.metaKey;
        }
    }
}

export {
  SpiceInputsConn,
  handle_mousemove,
  handle_mousedown,
  handle_contextmenu,
  handle_mouseup,
  handle_mousewheel,
  handle_keydown,
  handle_keyup,
  sendCtrlAltDel,
  sendText,
};
