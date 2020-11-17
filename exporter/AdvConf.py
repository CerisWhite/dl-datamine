import sys
import os
import pathlib
import json
import re
import itertools
from collections import defaultdict
from unidecode import unidecode
from tqdm import tqdm
from pprint import pprint
import argparse

from loader.Database import DBViewIndex, DBView, check_target_path
from exporter.Shared import ActionParts, PlayerAction, AbilityData
from exporter.Adventurers import CharaData, CharaUniqueCombo
from exporter.Dragons import DragonData
from exporter.Weapons import WeaponType, WeaponBody
from exporter.Wyrmprints import AbilityCrest, UnionAbility
from exporter.Mappings import WEAPON_TYPES, ELEMENTS, CLASS_TYPES, AFFLICTION_TYPES

ONCE_PER_ACT = ('sp', 'dp', 'utp', 'buff', 'afflic', 'bleed', 'extra', 'dispel')
DODGE_ACTIONS = {6, 40}
DEFAULT_AFF_DURATION = {
    'poison': 15,
    'burn': 12,
    'paralysis': 13,
    'frostbite': 21,
    'flashburn': 21,
    'blind': 8,
    'bog': 8,
    'freeze': 4.5,
    'stun': 6.5,
    'sleep': 4.5,
    'shadowblight': 21,
    'stormlash': 21
}

DEFAULT_AFF_IV = {
    'poison': 2.9,
    'burn': 3.9,
    'paralysis': 3.9,
    'frostbite': 2.9,
    'flashburn': 2.9,
    'shadowblight': 2.9,
    'stormlash': 2.9
}
DISPEL = 100

def snakey(name):
    return re.sub(r'[^0-9a-zA-Z ]', '', unidecode(name.replace('&', 'and')).strip()).replace(' ', '_')

def ele_bitmap(n):
    seq = 1
    while not n & 1 and n > 0:
        n = n >> 1
        seq += 1
    return ELEMENTS[seq]

def confsort(a):
    k, v = a
    if k[0] == 'x':
        try:
            return 'x'+k.split('_')[1]
        except IndexError:
            return k
    return k

INDENT = '    '
def fmt_conf(data, k=None, depth=0, f=sys.stdout, lim=2):
    if depth >= lim:
        if k == 'attr':
            r_str_lst = []
            end = len(data) - 1
            for idx, d in enumerate(data):
                if isinstance(d, int):
                    r_str_lst.append(' '+str(d))
                elif idx > 0:
                    r_str_lst.append('\n'+INDENT*(depth+1)+json.dumps(d))
                else:
                    r_str_lst.append(json.dumps(d))
            return '[\n' + INDENT*(depth+1) + (',').join(r_str_lst) + '\n' + INDENT*depth + ']' 
        return json.dumps(data)
    f.write('{\n')
    # f.write(INDENT*depth)
    end = len(data) - 1
    if depth == 0:
        items = enumerate(sorted(data.items(), key=confsort))
    else:
        items = enumerate(data.items())
    for idx, kv in items:
        k, v = kv
        f.write(INDENT*(depth+1))
        f.write('"')
        f.write(k)
        f.write('": ')
        res = fmt_conf(v, k, depth+1, f, lim)
        if res is not None:
            f.write(res)
        if idx < end:
            f.write(',\n')
        else:
            f.write('\n')
    f.write(INDENT*depth)
    f.write('}')

def fr(num):
    try:
        return round(num, 5)
    except TypeError:
        return 0

def clean_hitattr(attr, once_per_action):
    need_copy = False
    if once_per_action:
        for act in ONCE_PER_ACT:
            try:
                del attr[act]
                need_copy = True
            except KeyError:
                continue
    return attr, need_copy

def convert_all_hitattr(action, pattern=None, adv=None, skill=None):
    actparts = action['_Parts']
    hitattrs = []
    once_per_action = set()
    for part in actparts:
        part_hitattrs = []
        for label in ActionParts.HIT_LABELS:
            if (hitattr_lst := part.get(label)):
                if len(hitattr_lst) == 1:
                    hitattr_lst = hitattr_lst[0]
                if isinstance(hitattr_lst, dict):
                    if (attr := convert_hitattr(hitattr_lst, part, action, once_per_action, adv=adv, skill=skill)):
                        part_hitattrs.append(attr)
                elif isinstance(hitattr_lst, list):
                    for hitattr in hitattr_lst:
                        if (not pattern or pattern.match(hitattr['_Id'])) and \
                            (attr := convert_hitattr(hitattr, part, action, once_per_action, adv=adv, skill=skill)):
                            part_hitattrs.append(attr)
                            if not pattern:
                                break
        if not part_hitattrs:
            continue
        if (blt := part.get('_bulletNum', 0)) > 1 and not 'extra' in part_hitattrs[-1]:
            last_copy, need_copy = clean_hitattr(part_hitattrs[-1].copy(), once_per_action)
            if need_copy:
                part_hitattrs.append(last_copy)
                part_hitattrs.append(blt-1)
            else:
                part_hitattrs.append(blt)
        gen, delay = None, None
        if (gen := part.get('_generateNum')):
            delay = part.get('_generateDelay')
            ref_attrs = part_hitattrs
        elif (abd := part.get('_abDuration', 0)) > (abi := part.get('_abHitInterval', 0)):
            gen = int(abd/abi)
            delay = abi
            idx = -1
            while isinstance(part_hitattrs[idx], int):
                idx -= 1
            ref_attrs = [part_hitattrs[idx]]
        elif (bld := part.get('_bulletDuration', 0)) > (bci := part.get('_collisionHitInterval', 0)):
            gen = int(bld/bci) + 1
            delay = bci
            ref_attrs = [part_hitattrs[0]]
            # if adv is not None:
            #     print(adv.name)
        elif part.get('_loopFlag'):
            loopnum = part.get('_loopNum', 0)
            loopsec = part.get('_loopSec')
            delay = part.get('_seconds') + (part.get('_loopFrame', 0) / 60)
            if (loopsec := part.get('_loopSec')):
                gen = max(loopnum, int(loopsec // delay))
            else:
                gen = loopnum
            gen += 1
            ref_attrs = [part_hitattrs[0]] if len(part_hitattrs) == 1 else [part_hitattrs[1]]
        if gen and delay:
            gen_attrs = []
            for gseq in range(1, gen):
                for attr in ref_attrs:
                    gattr, _ = clean_hitattr(attr.copy(), once_per_action)
                    gattr['iv'] = fr(attr.get('iv', 0)+delay*gseq)
                    gen_attrs.append(gattr)
            part_hitattrs.extend(gen_attrs)
        hitattrs.extend(part_hitattrs)
    once_per_action = set()
    return hitattrs

def convert_hitattr(hitattr, part, action, once_per_action, adv=None, skill=None):
    attr = {}
    target = hitattr.get('_TargetGroup')
    if hitattr.get('_IgnoreFirstHitCheck'):
        once_per_action.clear()
    if target != 5 and hitattr.get('_DamageAdjustment'):
        attr['dmg'] = fr(hitattr.get('_DamageAdjustment'))
        killers = []
        for ks in ('_KillerState1', '_KillerState2', '_KillerState3'):
            if hitattr.get(ks):
                killers.append(hitattr.get(ks).lower())
        if len(killers) > 0:
            attr['killer'] = [fr(hitattr['_KillerStateDamageRate']-1), killers]
        if (crisis := hitattr.get('_CrisisLimitRate')):
            attr['crisis'] = fr(crisis-1)
        if (bufc := hitattr.get('_DamageUpRateByBuffCount')):
            attr['bufc'] = fr(bufc)
    if 'sp' not in once_per_action:
        if (sp := hitattr.get('_AdditionRecoverySp')):
            attr['sp'] = fr(sp)
            once_per_action.add('sp')
        elif (sp_p := hitattr.get('_RecoverySpRatio')):
            attr['sp'] = [fr(sp_p), '%']
            if (sp_i := hitattr.get('_RecoverySpSkillIndex')):
                attr['sp'].append(f's{sp_i}')
            once_per_action.add('sp')
    if 'dp' not in once_per_action and (dp := hitattr.get('_AdditionRecoveryDpLv1')):
        attr['dp'] = dp
        once_per_action.add('dp')
    if 'utp' not in once_per_action and ((utp := hitattr.get('_AddUtp')) or (utp := hitattr.get('_AdditionRecoveryUtp'))):
        attr['utp'] = utp
        once_per_action.add('utp')
    if (hp := hitattr.get('_HpDrainLimitRate')):
        attr['hp'] = fr(hp*100)
    if (cp := hitattr.get('_RecoveryCP')):
        attr['cp'] = cp
    # if hitattr.get('_RecoveryValue'):
    #     attr['heal'] = fr(hitattr.get('_RecoveryValue'))
    if part.get('commandType') == 'FIRE_STOCK_BULLET' and (stock := action.get('_MaxStockBullet', 0)) > 1:
        attr['extra'] = stock
    if (bc := attr.get('_DamageUpRateByBuffCount')):
        attr['bufc'] = bc
    if 0 < (attenuation := part.get('_attenuationRate', 0)) < 1:
        attr['fade'] = fr(attenuation)

    if (actcond := hitattr.get('_ActionCondition1')) and actcond['_Id'] not in once_per_action:
        once_per_action.add(actcond['_Id'])
        if actcond.get('_DamageLink'):
            return convert_hitattr(actcond['_DamageLink'], part, action, once_per_action, adv=adv, skill=skill)
        if actcond.get('_EfficacyType') == DISPEL and (rate := actcond.get('_Rate', 0)):
            attr['dispel'] = rate
        else:
            alt_buffs = []
            if adv and skill:
                for ehs in AdvConf.ENHANCED_SKILL:
                    if (esk := actcond.get(ehs)):
                        if isinstance(esk, int) or esk.get('_Id') in adv.all_chara_skills:
                            adv.chara_skill_loop.add(skill['_Id'])
                        else:
                            try:
                                s = int(ehs[-1])
                            except:
                                s = 1
                            eid = next(adv.eskill_counter)
                            group = 'enhanced' if eid == 1 else f'enhanced{eid}'
                            adv.chara_skills[esk.get('_Id')] = (f's{s}_{group}', s, esk, skill['_Id'])
                            alt_buffs.append(['sAlt', group, f's{s}'])
                if (eba := actcond.get('_EnhancedBurstAttack')) and isinstance(eba, dict):
                    eid = next(adv.efs_counter)
                    group = 'enhanced' if eid == 1 else f'enhanced{eid}'
                    adv.enhanced_fs.append((group, eba, eba.get('_BurstMarkerId')))
                    alt_buffs.append(['fsAlt', group])

            if target == 3 and (afflic := actcond.get('_Type')):
                affname = afflic.lower()
                attr['afflic'] = [affname, actcond['_Rate']]
                if (dot := actcond.get('_SlipDamagePower')):
                    attr['afflic'].append(fr(dot))
                duration = actcond.get('_DurationSec')
                duration = fr((duration + actcond.get('_MinDurationSec', duration)) / 2)
                if DEFAULT_AFF_DURATION[affname] != duration:
                    attr['afflic'].append(duration)
                    duration = None
                if (iv := actcond.get('_SlipDamageIntervalSec')):
                    iv = fr(iv)
                    if DEFAULT_AFF_IV[affname] != iv:
                        if duration:
                            attr['afflic'].append(duration)
                        attr['afflic'].append(iv)
            elif 'Bleeding' == actcond.get('_Text'):
                attr['bleed'] = [actcond['_Rate'], fr(actcond['_SlipDamagePower'])]
            else:
                buffs = []
                for tsn, btype in AdvConf.TENSION_KEY.items():
                    if (v := actcond.get(tsn)):
                        if target == 6:
                            buffs.append([btype, v, 'team'])
                        else:
                            buffs.append([btype, v])
                if not buffs:
                    if part.get('_lifetime'):
                        duration = fr(part.get('_lifetime'))
                        btype = 'zone'
                    elif actcond.get('_DurationNum') and not actcond.get('_DurationSec'):
                        duration = actcond.get('_DurationNum')
                        btype = 'next'
                    else:
                        duration = actcond.get('_DurationSec', -1)
                        duration = fr(duration)
                        btype = 'team' if target in (2, 6) else 'self'
                    for b in alt_buffs:
                        if btype == 'next' and b[0] == 'fsAlt':
                            b.extend((-1, duration))
                        elif duration > -1:
                            b.append(duration)
                        buffs.append(b)
                    if target == 3:
                        for k, mod in AdvConf.DEBUFFARG_KEY.items():
                            if (value := actcond.get(k)):
                                buffs.append(['debuff', fr(value), duration, actcond.get('_Rate')/100, mod])
                        for k, aff in AdvConf.AFFRES_KEY.items():
                            if (value := actcond.get(k)):
                                buffs.append(['affres', fr(value), duration, aff])
                    else:
                        for k, mod in AdvConf.BUFFARG_KEY.items():
                            if (value := actcond.get(k)):
                                if (bele := actcond.get('_TargetElemental')) and btype != 'self':
                                    buffs.append(['ele', fr(value), duration, *mod, ele_bitmap(bele).lower()])
                                elif k == '_SlipDamageRatio':
                                    buffs.append([btype, -fr(value), duration, *mod])
                                else:
                                    buffs.append([btype, fr(value), duration, *mod])
                if buffs:
                    if len(buffs) == 1:
                        buffs = buffs[0]
                    # if any(actcond.get(k) for k in AdvConf.OVERWRITE):
                    #     buffs.append('-refresh')
                    if actcond.get('_OverwriteGroupId'):
                        buffs.append(f'-overwrite_{actcond.get("_OverwriteGroupId")}')
                    elif actcond.get('_Overwrite'):
                        buffs.append('-refresh')
                    attr['buff'] = buffs
    if hitattr.get('_IgnoreFirstHitCheck'):
        once_per_action.clear()
    if attr:
        iv = fr(part['_seconds'] + part.get('_delayTime', 0))
        if iv > 0:
            attr['iv'] = iv
        if 'BULLET' in part['commandType']:
            attr['msl'] = 1
        return attr
    else:
        return None


def hit_sr(parts, seq=None, xlen=None, is_dragon=False, signal_end=None):
    s, r = None, None
    motion = None
    # use_motion = False
    timestop = 0
    timecurve = None
    followed_by = set()
    signals = {}
    signal_end = signal_end or set()
    motion_end = None
    for idx, part in enumerate(parts):
        if part['commandType'] == 'SEND_SIGNAL':
            actid = part.get('_actionId', 0)
            signals[actid] = -1
            if part.get('_motionEnd'):
                signal_end.add(actid)
        elif ('HIT' in part['commandType'] or 'BULLET' in part['commandType']) and s is None:
            s = fr(part['_seconds'])
        elif part['commandType'] == 'ACTIVE_CANCEL':
            recovery = part['_seconds']
            actid = part.get('_actionId')
            if seq and actid in signals:
                signals[actid] = recovery
            if (actid and part.get('_motionEnd')) or actid in signal_end:
                motion_end = recovery
            if act_cancel_id := part.get('_actionId'):
                followed_by.add((recovery, act_cancel_id))
        elif part['commandType'] == 'TIMESTOP':
            # timestop = part.get('_seconds', 0) + part.get('_duration', 0)
            ts_second = part.get('_seconds', 0)
            ts_delay = part.get('_duration', 0)
            timestop = ts_second + ts_delay
            # if is_dragon:
            #     found_hit = False
            #     for npart in parts[idx+1:]:
            #         if npart['_seconds'] > ts_second:
            #             # found_hit = found_hit or ('HIT' in npart['commandType'] or 'BULLET' in npart['commandType'])
            #             if 'HIT' in npart['commandType'] or 'BULLET' in npart['commandType']:
            #                 npart['_seconds'] += ts_delay
        elif is_dragon and part['commandType'] == 'TIMECURVE' and not part.get('_isNormalizeCurve'):
            timecurve = part.get('_duration')
        # if part['commandType'] == 'PARTS_MOTION' and part.get('_animation'):
        #     motion = fr(part['_animation']['stopTime'] - part['_blendDuration'])
        #     if part['_animation']['name'][0] == 'D':
        #         use_motion = True
    # if timestop > 0:
    # if not signal_to_next:
    #     for part in sorted(parts, key=lambda p: -p['_seq']):
    #         if part['commandType'] == 'ACTIVE_CANCEL' and part['_seconds'] > 0:
    #             r = part['_seconds']
    #             break
    # print(signals)
    if is_dragon and motion_end is not None:
        r = motion_end
    else:
        if timecurve is not None:
            for part in sorted(parts, key=lambda p: -p['_seq']):
                if part['commandType'] == 'ACTIVE_CANCEL' and part['_seconds'] > 0:
                    r = part['_seconds']
                    break
        else:
            try:
                r = max(signals.values())
            except:
                pass
            if r is None or r <= s:
                for part in reversed(parts):
                    if part['commandType'] == 'ACTIVE_CANCEL':
                        r = part['_seconds']
                        break
        if r is None:
            r = motion_end
        if r is not None:
            r = max(timestop, r)
    r = fr(r)
    return s, r, followed_by


def hit_attr_adj(action, s, conf, pattern=None, skip_nohitattr=True):
    if (hitattrs := convert_all_hitattr(action, pattern=pattern)):
        try:
            conf['recovery'] = fr(conf['recovery'] - s)
        except TypeError:
            conf['recovery'] = None
        for attr in hitattrs:
            if not isinstance(attr, int) and 'iv' in attr:
                attr['iv'] = fr(attr['iv'] - s)
                if attr['iv'] == 0:
                    del attr['iv']
        conf['attr'] = hitattrs
    if not hitattrs and skip_nohitattr:
        return None
    return conf


def convert_following_actions(startup, followed_by, default=None):
    interrupt_by = {}
    cancel_by = {}
    if default:
        for act in default:
            interrupt_by[act] = 0.0
            cancel_by[act] = 0.0
    for t, act in followed_by:
        if act in DODGE_ACTIONS:
            act_name = 'dodge'
        elif act % 10 == 5:
            act_name = 'fs'
        else:
            continue
        if t < startup:
            interrupt_by[act_name] = fr(t)
        else:
            cancel_by[act_name] = t
    cancel_by.update(interrupt_by)
    for act, t in cancel_by.items():
        cancel_by[act] = fr(max(0.0, t - startup))
    return interrupt_by, cancel_by


def convert_x(aid, xn, xlen=5, pattern=None, convert_follow=True, is_dragon=False):
    s, r, followed_by = hit_sr(xn['_Parts'], seq=aid, xlen=xlen, is_dragon=is_dragon)
    if s is None:
        pprint(xn)
    xconf = {
        'startup': s,
        'recovery': r
    }
    xconf = hit_attr_adj(xn, s, xconf, skip_nohitattr=False, pattern=pattern)
    
    if convert_follow:
        xconf['interrupt'], xconf['cancel'] = convert_following_actions(s, followed_by, ('s',))

    return xconf


def convert_fs(burst, marker=None, cancel=None):
    startup, recovery, followed_by = hit_sr(burst['_Parts'])
    fsconf = {}
    if not isinstance(marker, dict):
        fsconf['fs'] = hit_attr_adj(burst, startup, {'startup': startup, 'recovery': recovery}, re.compile(r'.*_LV02$'))
    else:
        mpart = marker['_Parts'][0]
        charge = mpart.get('_chargeSec', 0.5)
        fsconf['fs'] = {'charge': fr(charge), 'startup': startup, 'recovery': recovery}
        if (clv := mpart.get('_chargeLvSec')):
            clv = list(map(float, [charge]+json.loads(clv)))
            totalc = 0
            for idx, c in enumerate(clv):
                if idx == 0:
                    clv_attr = hit_attr_adj(burst, startup, fsconf[f'fs'].copy(), re.compile(f'.*_LV02$'))
                else:
                    clv_attr = hit_attr_adj(burst, startup, fsconf[f'fs'].copy(), re.compile(f'.*_LV02_CHLV0{idx+1}$'))
                totalc += c
                if clv_attr:
                    fsn = f'fs{idx+1}'
                    fsconf[fsn] = clv_attr
                    fsconf[fsn]['charge'] = fr(totalc)
                    fsconf[fsn]['interrupt'], fsconf[fsn]['cancel'] = convert_following_actions(startup, followed_by, ('s',))
            if 'fs2' in fsconf and 'attr' not in fsconf['fs']:
                del fsconf['fs']
            elif 'fs1' in fsconf:
                fsconf['fs'] = fsconf['fs1']
                del fsconf['fs1']
        else:
            fsconf['fs'] = hit_attr_adj(burst, startup, fsconf['fs'], re.compile(r'.*H0\d_LV02$'))
            fsconf['fs']['interrupt'], fsconf['fs']['cancel'] = convert_following_actions(startup, followed_by, ('s',))
    if cancel is not None:
        fsconf['fsf'] = {
            'charge': fr(0.1+cancel['_Parts'][0]['_duration']),
            'startup': 0.0,
            'recovery': 0.0,
        }
        fsconf['fsf']['interrupt'], fsconf['fsf']['cancel'] = convert_following_actions(startup, followed_by, ('s',))

    return fsconf


class BaseConf(WeaponType):
    LABEL_MAP = {
        'AXE': 'axe',
        'BOW': 'bow',
        'CAN': 'staff',
        'DAG': 'dagger',
        'KAT': 'blade',
        'LAN': 'lance',
        'ROD': 'wand',
        'SWD': 'sword',
        'GUN': 'gun'
    }
    GUN_MODES = (40, 41, 42)
    def process_result(self, res, exclude_falsy=True, full_query=True):
        conf = {'lv2':{}}
        if res['_Label'] != 'GUN':
            fs_id = res['_BurstPhase1']
            res = super().process_result(res, exclude_falsy=True, full_query=True)
            # fs_delay = {}
            fsconf = convert_fs(res['_BurstPhase1'], res['_ChargeMarker'], res['_ChargeCancel'])
            startup = fsconf['fs']['startup']
            # for x, delay in fs_delay.items():
            #     fsconf['fs'][x] = {'startup': fr(startup+delay)}
            conf.update(fsconf)
            for n in range(1, 6):
                try:
                    xn = res[f'_DefaultSkill0{n}']
                except KeyError:
                    break
                conf[f'x{n}'] = convert_x(xn['_Id'], xn)
                # for part in xn['_Parts']:
                #     if part['commandType'] == 'ACTIVE_CANCEL' and part.get('_actionId') == fs_id and part.get('_seconds'):
                #         fs_delay[f'x{n}'] = part.get('_seconds')
                if (hitattrs := convert_all_hitattr(xn, re.compile(r'.*H0\d_LV02$'))):
                    for attr in hitattrs:
                        attr['iv'] = fr(attr['iv'] - conf[f'x{n}']['startup'])
                        if attr['iv'] == 0:
                            del attr['iv']
                    conf['lv2'][f'x{n}'] = {'attr': hitattrs}
        else:
            # gun stuff
            for mode in BaseConf.GUN_MODES:
                mode = self.index['CharaModeData'].get(mode, exclude_falsy=exclude_falsy, full_query=True)
                mode_name = f'gun{mode["_GunMode"]}'
                if (burst := mode.get('_BurstAttackId')):
                    marker = burst.get('_BurstMarkerId')
                    if not marker:
                        marker = self.index['PlayerAction'].get(burst['_Id']+4, exclude_falsy=True)
                    for fs, fsc in convert_fs(burst, marker).items():
                        conf[f'{fs}_{mode_name}'] = fsc
                if (xalt := mode.get('_UniqueComboId')):
                    for prefix in ('', 'Ex'):
                        if xalt.get(f'_{prefix}ActionId'):
                            for n, xn in enumerate(xalt[f'_{prefix}ActionId']):
                                n += 1
                                xn_key = f'x{n}_{mode_name}{prefix.lower()}'
                                if xaltconf := convert_x(xn['_Id'], xn, xlen=xalt['_MaxComboNum']):
                                    conf[xn_key] = xaltconf
                                if (hitattrs := convert_all_hitattr(xn, re.compile(r'.*H0\d_LV02$'))):
                                    for attr in hitattrs:
                                        attr['iv'] = fr(attr['iv'] - conf[xn_key]['startup'])
                                        if attr['iv'] == 0:
                                            del attr['iv']
                                    conf['lv2'][xn_key] = {'attr': hitattrs}

        return conf

    @staticmethod
    def outfile_name(res, ext):
        return BaseConf.LABEL_MAP[res['_Label']]+ext

    def export_all_to_folder(self, out_dir='./out', ext='.json'):
        out_dir = os.path.join(out_dir, 'base')
        all_res = self.get_all(exclude_falsy=True)
        check_target_path(out_dir)
        for res in tqdm(all_res, desc=os.path.basename(out_dir)):
            out_name = self.outfile_name(res, ext)
            res = self.process_result(res, exclude_falsy=True)
            output = os.path.join(out_dir, out_name)
            with open(output, 'w', newline='', encoding='utf-8') as fp:
                # json.dump(res, fp, indent=2, ensure_ascii=False)
                fmt_conf(res, f=fp)

def convert_skill_common(skill, lv):
    action = skill.get('_AdvancedActionId1', 0)
    if isinstance(action, int):
        action = skill.get('_ActionId1')

    startup, recovery = 0.1, None
    actcancel = None
    mstate = None
    timestop = 0
    for part in action['_Parts']:
        if part['commandType'] == 'ACTIVE_CANCEL' and '_actionId' not in part and actcancel is None:
            actcancel = part['_seconds']
        if part['commandType'] == 'PARTS_MOTION' and mstate is None:
            if (animation := part.get('_animation')):
                if isinstance(animation, list):
                    mstate = sum(a['duration'] for a in animation)
                else:
                    mstate = animation['duration']
            if part.get('_motionState') in AdvConf.GENERIC_BUFF:
                mstate = 1.0
        if part['commandType'] == 'TIMESTOP':
            timestop = part['_seconds'] + part['_duration']
        if actcancel and mstate:
            break
    if actcancel:
        actcancel = max(timestop, actcancel)
    recovery = actcancel or mstate or recovery

    if recovery is None:
        AdvConf.MISSING_ENDLAG.append(skill.get('_Name'))

    sconf = {
        'sp': skill.get(f'_SpLv{lv}', skill.get('_Sp', 0)),
        'startup': startup,
        'recovery': None if not recovery else fr(recovery),
    }

    if nextaction := action.get('_NextAction'):
        for part in nextaction['_Parts']:
            part['_seconds'] += sconf['recovery'] or 0
        action['_Parts'].extend(nextaction['_Parts'])
        sconf['DEBUG_CHECK_NEXTACT'] = True

    return sconf, action

class AdvConf(CharaData):
    GENERIC_BUFF = ('skill_A', 'skill_B', 'skill_C', 'skill_D', 'skill_006_01')
    BUFFARG_KEY = {
        '_RateAttack': ('att', 'buff'),
        '_RateDefense': ('defense', 'buff'),
        '_RateHP': ('maxhp', 'buff'),
        '_RateCritical': ('crit', 'chance'),
        '_EnhancedCritical': ('crit', 'damage'),
        # '_RegenePower': ('heal', 'buff'),
        '_SlipDamageRatio': ('regen', 'buff'),
        '_RateRecoverySp': ('sp', 'passive'),
        # '_RateHP': ('hp', 'buff')
        '_RateAttackSpeed': ('spd', 'passive'),
        '_RateChargeSpeed': ('cspd', 'passive'),
        '_RateBurst': ('fs', 'buff'),
        '_RateSkill': ('s', 'buff'),
        # '_RateDamageShield': ('shield', 'buff')
    }
    DEBUFFARG_KEY = {
        '_RateDefense': 'def',
        '_RateDefenseB': 'defb',
        '_RateAttack': 'attack'
    }
    AFFRES_KEY = {
        '_RatePoison': 'poison',
        '_RateBurn': 'burn',
        '_RateFreeze': 'freeze',
        '_RateDarkness': 'blind',
        '_RateSwoon': 'stun',
        '_RateSlowMove': 'bog',
        '_RateSleep': 'sleep',
        '_RateFrostbite': 'frostbite',
        '_RateFlashheat': 'flashburn'
    }
    TENSION_KEY = {
        '_Tension': 'energy',
        '_Inspiration': 'inspiration'
    }
    # OVERWRITE = ('_Overwrite', '_OverwriteVoice', '_OverwriteGroupId')
    ENHANCED_SKILL = ('_EnhancedSkill1', '_EnhancedSkill2')

    MISSING_ENDLAG = []
    DO_NOT_FIND_LOOP = (
        10350302, # summer norwin
        10650101, # gala sarisse
    )

    def convert_skill(self, k, seq, skill, lv):
        sconf, action = convert_skill_common(skill, lv)

        if (hitattrs := convert_all_hitattr(action, re.compile(f'.*LV0{lv}$'), adv=self, skill=skill)):
            sconf['attr'] = hitattrs
        if (not hitattrs or all(['dmg' not in attr for attr in hitattrs if isinstance(attr, dict)])) and skill.get(f'_IsAffectedByTensionLv{lv}'):
            sconf['energizable'] = bool(skill[f'_IsAffectedByTensionLv{lv}'])

        if (transkills := skill.get('_TransSkill')) and isinstance(transkills, dict):
            k = f's{seq}_phase1'
            for idx, ts in enumerate(transkills.items()):
                tsid, tsk = ts
                if tsid not in self.all_chara_skills:
                    self.chara_skills[tsid] = (f's{seq}_phase{idx+1}', seq, tsk, skill.get('_Id'))

        if (ab := skill.get(f'_Ability{lv}')):
            if isinstance(ab, int):
                ab = self.index['AbilityData'].get(ab, exclude_falsy=True)
            for a in (1, 2, 3):
                if ab.get('_AbilityType1') == 44: # alt skill
                    s = int(ab['_TargetAction1'][-1])
                    eid = next(self.eskill_counter)
                    group = 'enhanced' if eid == 1 else f'enhanced{eid}'
                    self.chara_skills[ab[f'_VariousId1a']['_Id']] = (f's{s}_{group}', s, ab[f'_VariousId1a'], skill['_Id'])
        return sconf, k

    def process_result(self, res, exclude_falsy=True, condense=True):
        self.index['ActionParts'].animation_reference = ('CharacterMotion', int(f'{res["_BaseId"]:06}{res["_VariationId"]:02}'))
        self.chara_skills = {}
        self.chara_skill_loop = set()
        self.eskill_counter = itertools.count(start=1)
        self.efs_counter = itertools.count(start=1)
        self.all_chara_skills = {}
        self.enhanced_fs = []
        self.ab_alt_buffs = defaultdict(lambda: [])

        ab_lst = []
        for i in (1, 2, 3):
            for j in (3, 2, 1):
                if (ab := res.get(f'_Abilities{i}{j}')):
                    ab_lst.append(self.index['AbilityData'].get(ab, full_query=True, exclude_falsy=exclude_falsy))
                    break
        converted, skipped = convert_all_ability(ab_lst)
        res = self.condense_stats(res)
        conf = {
            'c': {
                'name': res.get('_SecondName', res['_Name']),
                'icon': f'{res["_BaseId"]:06}_{res["_VariationId"]:02}_r{res["_Rarity"]:02}',
                'att': res['_MaxAtk'],
                'hp': res['_MaxHp'],
                'ele': ELEMENTS[res['_ElementalType']].lower(),
                'wt': WEAPON_TYPES[res['_WeaponType']].lower(),
                'spiral': res['_MaxLimitBreakCount'] == 5,
                'a': converted,
                'skipped': skipped
            }
        }
        if conf['c']['wt'] == 'gun':
            conf['c']['gun'] = []
        self.name = conf['c']['name']

        for ab in ab_lst:
            for i in (1, 2, 3):
                # enhanced s/fs buff
                group = None
                if ab.get(f'_AbilityType{i}') == 14:
                    unique_name = snakey(self.name.lower()).replace('_', '')
                    actcond = ab.get(f'_VariousId{i}a')
                    if not actcond:
                        actcond = ab.get(f'_VariousId{i}str')
                    sid = ab.get('_OnSkill')
                    cd = actcond.get('_CoolDownTimeSec')
                    for ehs in AdvConf.ENHANCED_SKILL:
                        if (esk := actcond.get(ehs)):
                            try:
                                s = int(ehs[-1])
                            except:
                                s = 1
                            eid = next(self.eskill_counter)
                            if group is None:
                                group = unique_name if eid == 1 else f'{unique_name}{eid}'
                            self.chara_skills[esk.get('_Id')] = (f's{s}_{group}', s, esk, None)
                            if sid and not cd:
                                self.ab_alt_buffs[sid].append(['sAlt', group, f's{s}', -1, actcond.get('_DurationNum', 0)])
                    if (eba := actcond.get('_EnhancedBurstAttack')) and isinstance(eba, dict):
                        eid = next(self.efs_counter)
                        group = unique_name if eid == 1 else f'{unique_name}{eid}'
                        self.enhanced_fs.append((group, eba, eba.get('_BurstMarkerId')))
                        if sid and not cd:
                            self.ab_alt_buffs[sid].append(['fsAlt', group])
                    for b in self.ab_alt_buffs[sid]:
                        if dnum := actcond.get('_DurationNum'):
                            b.extend((-1, dnum))
                        elif dtime := actcond.get('_Duration'):
                            b.append(dtime)


        if (burst := res.get('_BurstAttack')):
            burst = self.index['PlayerAction'].get(res['_BurstAttack'], exclude_falsy=exclude_falsy)
            if burst and (marker := burst.get('_BurstMarkerId')):
                conf.update(convert_fs(burst, marker))

        # exceptions exist
        if conf['c']['spiral']:
            mlvl = {1: 4, 2: 3}
        else:
            mlvl = {1: 3, 2: 2}
        for s in (1, 2):
            skill = self.index['SkillData'].get(res[f'_Skill{s}'], 
                exclude_falsy=exclude_falsy, full_query=True)
            self.chara_skills[res[f'_Skill{s}']] = (f's{s}', s, skill, None)
        if (edit := res.get('_EditSkillId')) and edit not in self.chara_skills:
            skill = self.index['SkillData'].get(res[f'_EditSkillId'], 
                exclude_falsy=exclude_falsy, full_query=True)
            self.chara_skills[res['_EditSkillId']] = (f's99', 99, skill, None)

        for m in range(1, 5):
            if (mode := res.get(f'_ModeId{m}')):
                mode = self.index['CharaModeData'].get(mode, exclude_falsy=exclude_falsy, full_query=True)
                if not mode:
                    continue
                if (gunkind := mode.get('_GunMode')):
                    conf['c']['gun'].append(gunkind)
                    if not any([mode.get(f'_Skill{s}Id') for s in (1, 2)]):
                        continue
                try:
                    mode_name = unidecode(mode['_ActionId']['_Parts'][0]['_actionConditionId']['_Text'].split(' ')[0].lower())
                except:
                    if res.get('_ModeChangeType') == 3:
                        mode_name = 'ddrive'
                    else:
                        mode_name = f'mode{m}'
                for s in (1, 2):
                    if (skill := mode.get(f'_Skill{s}Id')):
                        self.chara_skills[skill.get('_Id')] = (f's{s}_{mode_name}', s, skill, None)
                if (burst := mode.get('_BurstAttackId')):
                    marker = burst.get('_BurstMarkerId')
                    if not marker:
                        marker = self.index['PlayerAction'].get(burst['_Id']+4, exclude_falsy=True)
                    for fs, fsc in convert_fs(burst, marker).items():
                        conf[f'{fs}_{mode_name}'] = fsc
                if (xalt := mode.get('_UniqueComboId')):
                    xalt_pattern = re.compile(r'.*H0\d_LV02$') if conf['c']['spiral'] else None
                    for prefix in ('', 'Ex'):
                        if xalt.get(f'_{prefix}ActionId'):
                            for n, xn in enumerate(xalt[f'_{prefix}ActionId']):
                                n += 1
                                if xaltconf := convert_x(xn['_Id'], xn, xlen=xalt['_MaxComboNum'], pattern=xalt_pattern):
                                    conf[f'x{n}_{mode_name}{prefix.lower()}'] = xaltconf
                                elif xalt_pattern is not None and (xaltconf := convert_x(xn['_Id'], xn, xlen=xalt['_MaxComboNum'])):
                                    conf[f'x{n}_{mode_name}{prefix.lower()}'] = xaltconf
        try:
            conf['c']['gun'] = list(set(conf['c']['gun']))
        except KeyError:
            pass

        # self.abilities = self.last_abilities(res, as_mapping=True)
        # pprint(self.abilities)
        # for k, seq, skill in self.chara_skills.values():
        while self.chara_skills:
            k, seq, skill, prev_id = next(iter(self.chara_skills.values()))
            self.all_chara_skills[skill.get('_Id')] = (k, seq, skill, prev_id)
            if seq == 99:
                lv = mlvl[res['_EditSkillLevelNum']]
            else:
                lv = mlvl[seq]
            cskill, k = self.convert_skill(k, seq, skill, lv)
            conf[k] = cskill
            if (ab_alt_buffs := self.ab_alt_buffs.get(seq)):
                if len(ab_alt_buffs) == 1:
                    ab_alt_buffs = [ab_alt_buffs[0], '-refresh']
                else:
                    ab_alt_buffs = [*ab_alt_buffs, '-refresh']
                if 'attr' not in conf[k]:
                    conf[k]['attr'] = []
                conf[k]['attr'].append({'buff': ab_alt_buffs})
            del self.chara_skills[skill.get('_Id')]

        for efs, eba, emk in self.enhanced_fs:
            n = ''
            for fs, fsc in convert_fs(eba, emk).items():
                conf[f'{fs}_{efs}'] = fsc

        if res.get('_Id') not in AdvConf.DO_NOT_FIND_LOOP:
            if self.chara_skill_loop:
                for loop_id in self.chara_skill_loop:
                    k, seq, _, prev_id = self.all_chara_skills.get(loop_id)
                    loop_sequence = [(k, seq)]
                    while prev_id != loop_id and prev_id is not None:
                        k, seq, _, pid = self.all_chara_skills.get(prev_id)
                        loop_sequence.append((k, seq))
                        prev_id = pid
                    for p, ks in enumerate(reversed(loop_sequence)):
                        k, seq = ks
                        conf[f's{seq}_phase{p+1}'] = conf[k]
                        del conf[k]

        if (udrg := res.get('_UniqueDragonId')):
            conf['dragonform'] = self.index['DrgConf'].get(udrg, by='_Id')
            del conf['dragonform']['d']

        return conf

    def get(self, name):
        res = super().get(name, full_query=False)
        if isinstance(res, list):
            res = res[0]
        return self.process_result(res)

    @staticmethod
    def outfile_name(conf, ext):
        return snakey(conf['c']['name']) + ext

    def export_all_to_folder(self, out_dir='./out', ext='.json'):
        all_res = self.get_all(exclude_falsy=True, where='_ElementalType != 99 AND _IsPlayable = 1')
        ref_dir = os.path.join(out_dir, '..', 'adv')
        out_dir = os.path.join(out_dir, 'adv')
        check_target_path(out_dir)
        for res in tqdm(all_res, desc=os.path.basename(out_dir)):
            if not res.get('_IsPlayable'):
                continue
            try:
                outconf = self.process_result(res, exclude_falsy=True)
                out_name = self.outfile_name(outconf, ext)
                output = os.path.join(out_dir, out_name)
                # ref = os.path.join(ref_dir, out_name)
                # if os.path.exists(ref):
                #     with open(ref, 'r', newline='', encoding='utf-8') as fp:
                #         refconf = json.load(fp)
                #         try:
                #             outconf['c']['a'] = refconf['c']['a']
                #         except:
                #             outconf['c']['a'] = []
                with open(output, 'w', newline='', encoding='utf-8') as fp:
                    # json.dump(res, fp, indent=2, ensure_ascii=False)
                    fmt_conf(outconf, f=fp)
            except Exception as e:
                print(res['_Id'])
                pprint(outconf)
                raise e
        print('Missing endlag for:', AdvConf.MISSING_ENDLAG)


def ab_cond(ab):
    cond = ab.get('_ConditionType')
    condval = ab.get('_ConditionValue')
    ele = ab.get('_ElementalType')
    wep = ab.get('_WeaponType')
    cparts = []
    if ele:
        cparts.append(ele.lower())
    if wep:
        cparts.append(wep.lower())
    if condval:
        condval = int(condval)
    if cond == 'hp geq':
        cparts.append(f'hp{condval}')
    elif cond == 'hp leq':
        cparts.append(f'hp≤{condval}')
    elif cond == 'combo':
        cparts.append(f'hit{condval}')
    if cparts:
        return '_'.join(cparts)


AB_STATS = {
    1: 'hp',
    2: 'a',
    4: 'sp',
    5: 'dh',
    8: 'dt',
    10: 'spd',
    12: 'cspd'
}
def ab_stats(**kwargs):
    if (stat := AB_STATS.get(kwargs.get('var_a'))) and (upval := kwargs.get('upval')):
        res = [stat, upval/100]
        if (condstr := ab_cond(kwargs.get('ab'))):
            res.append(condstr)
        return res

def ab_aff_edge(**kwargs):
    if (a_id := kwargs.get('var_a')):
        return [f'edge_{AFFLICTION_TYPES.get(a_id, a_id).lower()}', kwargs.get('upval')]

def ab_damage(**kwargs):
    if upval := kwargs.get('upval'):
        res = None
        target = kwargs.get('target')
        astr = None
        if target == 'skill':
            astr = 's'
        elif target == 'force strike':
            astr = 'fs'
        if astr:
            res = [astr, upval/100]
        else:
            cond = kwargs.get('ab').get('_ConditionType')
            if cond == 'bleed':
                res = ['bleed', upval/100]
            elif cond == 'overdrive':
                res = ['od', upval/100]
            elif cond == 'break':
                res = ['bk', upval/100]
        condstr = ab_cond(kwargs.get('ab'))
        if res:
            if condstr:
                res.append(condstr)
            return res

def ab_actcond(**kwargs):
    ab = kwargs['ab']
    # special case FS prep
    actcond = kwargs.get('var_a')
    if not actcond:
        if (var_str := kwargs.get('var_str')):
            actcond = var_str.get('_ActionCondition1')
    cond = ab.get('_ConditionType')
    astr = None
    extra_args = []
    if cond == 'doublebuff':
        if (cd := kwargs.get('_CoolTime')):
            astr = 'bcc'
        else:
            astr = 'bc'
    elif cond == 'hp drop under':
        if ab.get('_OccurenceNum'):
            astr = 'lo'
        else:
            astr = 'ro'
    elif cond == 'every combo':
        if ab.get('_TargetAction') == 'force strike':
            return ['fsprep', ab.get('_OccurenceNum'), kwargs.get('var_str').get('_RecoverySpRatio')]
        if (val := actcond.get('_Tension')):
            return ['ecombo', int(ab.get('_ConditionValue'))]
    elif cond == 'prep' and (val := actcond.get('_Tension')):
        return ['eprep', int(val)]
    elif cond == 'claws':
        if val := actcond.get('_RateSkill'):
            return ['dcs', 3]
        elif val := actcond.get('_RateDefense'):
            return ['dcd', 3]
        else:
            return ['dc', 3]
    elif cond == 'primed':
        astr = 'primed'
    elif cond == 'slayer/striker':
        if ab.get('_TargetAction') == 'force strike':
            astr = 'sts'
        else:
            astr = 'sls'
    elif cond == 'affliction proc':
        affname = AFFLICTION_TYPES[ab.get('_ConditionValue')].lower()
        if var_str.get('_TargetGroup') == 6:
            astr = f'affteam_{affname}'
        else:
            astr = f'affself_{affname}'
        if (duration := actcond.get('_DurationSec')) != 15:
            extra_args.append(duration)
        if (cooltime := ab.get('_CoolTime')) != 10:
            if not extra_args:
                extra_args.append(fr(actcond.get('_DurationSec')))
            extra_args.append(fr(cooltime))
    if astr:
        full_astr, value = None, None
        if (val := actcond.get('_Tension')):
            full_astr = f'{astr}_energy'
            value = int(val)
        elif (att := actcond.get('_RateAttack')):
            full_astr = f'{astr}_att'
            value = fr(att)
        elif (cchance := actcond.get('_RateCritical')):
            full_astr = f'{astr}_crit_chance'
            value = fr(cchance)
        elif (cdmg := actcond.get('_EnhancedCritical')):
            full_astr = f'{astr}_crit_damage'
            value = fr(cdmg)
        elif (defence := actcond.get('_RateDefense')):
            full_astr = f'{astr}_defense'
            value = fr(defence)
        elif (regen := actcond.get('_SlipDamageRatio')):
            full_astr = f'{astr}_regen'
            value = fr(regen*-100)
        if full_astr and value:
            return [full_astr, value, *extra_args]


def ab_generic(name, div=None):
    def ab_whatever(**kwargs):
        if (upval := kwargs.get('upval')):
            res = [name, upval if not div else upval/div]
            if (condstr := ab_cond(kwargs.get('ab'))):
                res.append(condstr)
            return res
    return ab_whatever

def ab_aff_k(**kwargs):
    if (a_id := kwargs.get('var_a')):
        res = [f'k_{AFFLICTION_TYPES.get(a_id, a_id).lower()}', kwargs.get('upval')/100]
        if (condstr := ab_cond(kwargs.get('ab'))):
            res.append(condstr)
        return res


ABILITY_CONVERT = {
    1: ab_stats,
    3: ab_aff_edge,
    6: ab_damage,
    7: ab_generic('cc', 100),
    11: ab_generic('spf', 100),
    14: ab_actcond,
    17: ab_generic('prep'),
    18: ab_generic('bt', 100),
    19: ab_generic('dbt', 100),
    20: ab_aff_k,
    26: ab_generic('cd', 100),
    27: ab_generic('dp'),
    36: ab_generic('da', 100),
    59: ab_generic('dbt', 100) # ?
}
SPECIAL = {
    448: ['spu', 0.08],
    1402: ['au', 0.08]
}
def convert_ability(ab, debug=False):
    if special_ab := SPECIAL.get(ab.get('_Id')):
        return [special_ab], []
    converted = []
    skipped = []
    for i in (1, 2, 3):
        if not f'_AbilityType{i}' in ab:
            continue
        atype = ab[f'_AbilityType{i}']
        if (convert_a := ABILITY_CONVERT.get(atype)):
            try:
                res = convert_a(
                    # atype=atype,
                    # cond=ab.get('_ConditionType'),
                    # condval=ab.get('_ConditionValue'),
                    # ele=ab.get('_ElementalType'),
                    # wep=ab.get('_WeaponType'),
                    # cd=ab.get('_CoolTime'),
                    ab=ab,
                    target=ab.get(f'_TargetAction{i}'),
                    upval=ab.get(f'_AbilityType{i}UpValue'),
                    var_a=ab.get(f'_VariousId{i}a'),
                    var_b=ab.get(f'_VariousId{i}b'),
                    var_c=ab.get(f'_VariousId{i}c'),
                    var_str=ab.get(f'_VariousId{i}str'),
                )
            except:
                res = None
            if res:
                converted.append(res)
        elif atype == 43:
            for a in ('a', 'b', 'c'):
                if (subab := ab.get(f'_VariousId{i}{a}')):
                    sub_c, sub_s = convert_ability(subab)
                    converted.extend(sub_c)
                    skipped.extend(sub_s)
    if debug or not converted:
        skipped.append((ab.get('_Id'), ab.get('_Name')))
    return converted, skipped


def convert_all_ability(ab_lst, debug=False):
    all_c, all_s = [], []
    for ab in ab_lst:
        converted, skipped = convert_ability(ab, debug=debug)
        all_c.extend(converted)
        all_s.extend(skipped)
    return all_c, all_s

# ALWAYS_KEEP = {400127, 400406, 400077, 400128, 400092, 400410}
class WpConf(AbilityCrest):
    HDT_PRINT = {
        "name": "High Dragon Print",
        "icon": "HDT",
        "hp": 83,
        "att": 20,
        "rarity": 5,
        "union": 0,
        "a": [["res_hdt", 0.25]]
    }
    SKIP_BOON = (0, 7, 8, 9, 10)
    def process_result(self, res, exclude_falsy=True):
        ab_lst = []
        for i in (1, 2, 3):
            k = f'_Abilities{i}3'
            if (ab := res.get(k)):
                ab_lst.append(self.index['AbilityData'].get(ab, full_query=True, exclude_falsy=exclude_falsy))            
        converted, skipped = convert_all_ability(ab_lst)

        boon = res.get('_UnionAbilityGroupId', 0)
        if boon in WpConf.SKIP_BOON:
            if not converted:
                return
            if converted[0][0].startswith('sts') or converted[0][0].startswith('sls'):
                return

        conf = {
            'name': res['_Name'].strip(),
            'icon': f'{res["_BaseId"]}_02',
            'att': res['_MaxAtk'],
            'hp': res['_MaxHp'],
            'rarity': res['_Rarity'],
            'union': boon,
            'a': converted,
            # 'skipped': skipped
        }
        return conf

    def export_all_to_folder(self, out_dir='./out', ext='.json'):
        all_res = self.get_all(exclude_falsy=True)
        check_target_path(out_dir)
        outdata = {}
        skipped = []
        for res in tqdm(all_res, desc=os.path.basename(out_dir)):
            conf = self.process_result(res, exclude_falsy=True)
            if conf:
                outdata[snakey(res['_Name'])] = conf
            else:
                skipped.append((res['_BaseId'], res['_Name']))
                # skipped.append(res["_Name"])
        outdata['High_Dragon_Print'] = WpConf.HDT_PRINT
        output = os.path.join(out_dir, 'wyrmprints.json')
        with open(output, 'w', newline='', encoding='utf-8') as fp:
            # json.dump(res, fp, indent=2, ensure_ascii=False)
            fmt_conf(outdata, f=fp)
        print('Skipped:', skipped)

    def get(self, name):
        res = super().get(name, full_query=False)
        if isinstance(res, list):
            res = res[0]
        return self.process_result(res)
    

class DrgConf(DragonData):
    EXTRA_DRAGONS = (
        20050102,
        20050202,
        20050302,
        20050402,
        20050502,
        20050507,
    )
    COMMON_ACTIONS = {'dodge': {}, 'dodgeb': {}, 'dshift': {}}
    COMMON_ACTIONS_DEFAULTS = {
        # recovery only
        'dodge': 0.66667,
        'dodgeb': 0.66667,
        'dshift': 0.69444,
    }
    def process_result(self, res, exclude_falsy=True):
        super().process_result(res, exclude_falsy)

        ab_lst = []
        for i in (1, 2):
            if (ab := res.get(f'_Abilities{i}5')):
                ab_lst.append(ab)
        converted, skipped = convert_all_ability(ab_lst)

        conf = {
            'd': {
                'name': res.get('_SecondName', res['_Name']),
                'icon': f'{res["_BaseId"]}_{res["_VariationId"]:02}',
                'att': res['_MaxAtk'],
                'hp': res['_MaxHp'],
                'ele': ELEMENTS.get(res['_ElementalType']).lower(),
                'a': converted
            }
        }
        if skipped:
            conf['d']['skipped'] = skipped

        for act, key in (('dodge', '_AvoidActionFront'), ('dodgeb', '_AvoidActionBack'), ('dshift', '_Transform')):
            s, r, _ = hit_sr(res[key]['_Parts'], is_dragon=True, signal_end={None})
            try:
                DrgConf.COMMON_ACTIONS[act][r].add(conf['d']['name'])
            except KeyError:
                DrgConf.COMMON_ACTIONS[act][r] = {conf['d']['name']}
            if DrgConf.COMMON_ACTIONS_DEFAULTS[act] != r:
                conf[act] = {'recovery': r}
        
        if 'dodgeb' in conf:
            if 'dodge' not in conf or conf['dodge']['recovery'] > conf['dodgeb']['recovery']:
                conf['dodge'] = conf['dodgeb']
                conf['dodge']['backdash'] = True
            del conf['dodgeb']

        dcombo = res['_DefaultSkill']
        dcmax = res['_ComboMax']
        for n, xn in enumerate(dcombo):
            n += 1
            if dxconf := convert_x(xn['_Id'], xn, xlen=dcmax, convert_follow=False, is_dragon=True):
                conf[f'dx{n}'] = dxconf

        for act, key in (('ds', '_Skill1'), ('ds_final', '_SkillFinalAttack')):
            if not (dskill := res.get(key)):
                continue
            sconf, action = convert_skill_common(dskill, 2)
            sconf['uses'] = dskill.get('_MaxUseNum', 1)
            
            if (hitattrs := convert_all_hitattr(action, re.compile(r'.*LV02$'))):
                sconf['attr'] = hitattrs
            if (not hitattrs or all(['dmg' not in attr for attr in hitattrs if isinstance(attr, dict)])) and dskill.get(f'_IsAffectedByTensionLv2'):
                sconf['energizable'] = bool(dskill['_IsAffectedByTensionLv2'])

            conf[act] = sconf

        return conf

    def export_all_to_folder(self, out_dir='./out', ext='.json'):
        where_str = '_Rarity = 5 AND _IsPlayable = 1 AND (_SellDewPoint = 8500 OR _Id in ('+ ','.join(map(str, DrgConf.EXTRA_DRAGONS)) +')) AND _Id = _EmblemId'
        all_res = self.get_all(exclude_falsy=True, where=where_str)
        out_dir = os.path.join(out_dir, 'drg')
        check_target_path(out_dir)
        outdata = {
            'flame': {},
            'water': {},
            'wind': {},
            'light': {},
            'shadow': {}
        }
        # skipped = []
        for res in tqdm(all_res, desc=os.path.basename(out_dir)):
            conf = self.process_result(res, exclude_falsy=True)
            # outfile = snakey(conf['d']['ele']) + '.json'
            if conf:
                outdata[conf['d']['ele']][snakey(conf['d']['name'])] = conf
        for ele, data in outdata.items():
            output = os.path.join(out_dir, f'{ele}.json')
            with open(output, 'w', newline='', encoding='utf-8') as fp:
                fmt_conf(data, f=fp, lim=3)
        #     else:
        #         skipped.append(res["_Name"])
        # pprint(DrgConf.COMMON_ACTIONS)

    def get(self, name, by=None):
        res = super().get(name, by=by, full_query=False)
        if isinstance(res, list):
            res = res[0]
        return self.process_result(res)


class WepConf(WeaponBody):
    T2_ELE = ('shadow', 'flame')
    def process_result(self, res, exclude_falsy=True):
        super().process_result(res, exclude_falsy)
        skin = res['_WeaponSkinId']
        # if skin['_FormId'] % 10 == 1 and res['_ElementalType'] in WepConf.T2_ELE:
        #     return None
        tier = res.get('_MaxLimitOverCount', 0) + 1
        conf = {
            'name': res['_Name'],
            'icon': f'{skin["_BaseId"]}_{skin["_VariationId"]:02}_{skin["_FormId"]}',
            'att': res[f'_MaxAtk{tier}'],
            'hp': res[f'_MaxHp{tier}'],
            'ele': res['_ElementalType'].lower(),
            'wt': res['_WeaponType'].lower(),
            'tier': tier
        }
        return conf

    def export_all_to_folder(self, out_dir='./out', ext='.json'):
        all_res = self.get_all(exclude_falsy=True, where='_WeaponSeriesId = 4')
        check_target_path(out_dir)
        outdata = {
            'flame': {},
            'water': {},
            'wind': {},
            'light': {},
            'shadow': {}
        }
        # skipped = []
        for res in tqdm(all_res, desc=os.path.basename(out_dir)):
            conf = self.process_result(res, exclude_falsy=True)
            # outfile = snakey(conf['d']['ele']) + '.json'
            if conf:
                outdata[conf['ele']][conf['wt']] = conf
        output = os.path.join(out_dir, f'weapons.json')
        with open(output, 'w', newline='', encoding='utf-8') as fp:
            fmt_conf(outdata, f=fp)
        #     else:
        #         skipped.append(res["_Name"])
        # print('Skipped:', ','.join(skipped))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-a', help='_Name/_SecondName')
    parser.add_argument('-d', help='_Name')
    parser.add_argument('-wp', help='_Name')
    parser.add_argument('-s', help='_SkillId')
    parser.add_argument('-slv', help='Skill level')
    parser.add_argument('-f', help='_BurstAttackId')
    parser.add_argument('-x', help='_UniqueComboId')
    parser.add_argument('-fm', help='_BurstAttackMarker')
    # parser.add_argument('-x', '_UniqueComboId')
    parser.add_argument('-w', help='_Name')
    parser.add_argument('-act', help='_ActionId')
    args = parser.parse_args()

    index = DBViewIndex()
    # out_dir = os.path.join(pathlib.Path(__file__).parent.absolute(), '..', '..', 'dl', 'conf', 'gen')
    out_dir = os.path.join(pathlib.Path(__file__).parent.absolute(), '..', 'out', 'gen')

    if args.s and args.slv:
        view = AdvConf(index)
        if args.a:
            view.get(args.a)
        view.eskill_counter = itertools.count(start=1)
        view.efs_counter = itertools.count(start=1)
        view.chara_skills = {}
        view.enhanced_fs = []
        view.chara_skill_loop = set()
        sconf, k = view.convert_skill('s1', 1, view.index['SkillData'].get(int(args.s), exclude_falsy=True), int(args.slv))
        sconf = {k: sconf}
        fmt_conf(sconf, f=sys.stdout)
        print('\n')
        pprint(view.chara_skills.keys())
        pprint(view.enhanced_fs)
    elif args.a:
        view = AdvConf(index)
        if args.a == 'all':
            view.export_all_to_folder(out_dir=out_dir)
        else:
            fmt_conf(view.get(args.a), f=sys.stdout)
    elif args.d:
        view = DrgConf(index)
        if args.d == 'all':
            view.export_all_to_folder(out_dir=out_dir)
        else:
            d = view.get(args.d)
            fmt_conf(d, f=sys.stdout)
    elif args.wp:
        view = WpConf(index)
        if args.wp == 'all':
            view.export_all_to_folder(out_dir=out_dir)
        else:
            wp = view.get(args.wp)
            fmt_conf({snakey(wp['name']): wp}, f=sys.stdout)
    elif args.f:
        view = PlayerAction(index)
        burst = view.get(int(args.f), exclude_falsy=True)
        if (mid := burst.get('_BurstMarkerId')):
            marker = mid
        elif args.fm:
            marker = view.get(int(args.fm), exclude_falsy=True)
        else:
            marker = view.get(int(args.f)+4, exclude_falsy=True)
        fmt_conf(convert_fs(burst, marker), f=sys.stdout)
    elif args.x:
        view = CharaUniqueCombo(index)
        xalt = view.get(int(args.x), exclude_falsy=True)
        conf = {}
        for prefix in ('', 'Ex'):
            if xalt.get(f'_{prefix}ActionId'):
                for n, xn in enumerate(xalt[f'_{prefix}ActionId']):
                    n += 1
                    if xaltconf := convert_x(xn['_Id'], xn, xlen=xalt['_MaxComboNum']):
                        conf[f'x{n}_{prefix.lower()}'] = xaltconf
        fmt_conf(conf, f=sys.stdout)
    elif args.w:
        if args.w == 'base':
            view = BaseConf(index)
            view.export_all_to_folder(out_dir=out_dir)
        elif args.w == 'all':
            view = WepConf(index)
            view.export_all_to_folder(out_dir=out_dir)
    elif args.act:
        view = PlayerAction(index)
        action = view.get(int(args.act), exclude_falsy=True)
        pprint(hit_sr(action['_Parts'], is_dragon=True))