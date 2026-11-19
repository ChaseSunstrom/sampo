"""Builds ../index.html from page.html, page.js and the drawings in art.py, hero.py and stations.py."""
import base64, html, json, os, shutil, sys
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import art, hero, stations

MARK = os.path.join(HERE, '..')
def uri(name): return 'data:image/svg+xml;base64,' + base64.b64encode(open(os.path.join(MARK, name), 'rb').read()).decode()

EXTRA_DEFS = '''
<linearGradient id="seamU" x1="40" x2="216" y1="0" y2="0" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#7A1E10"/><stop offset=".18" stop-color="#E2481A"/><stop offset=".38" stop-color="#FFB03C"/><stop offset=".52" stop-color="#FFF8E6"/><stop offset=".66" stop-color="#FFB03C"/><stop offset=".86" stop-color="#E2481A"/><stop offset="1" stop-color="#7A1E10"/></linearGradient>
<radialGradient id="forgeSide" cx="20" cy="240" r="230" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#FF8A2A" stop-opacity=".34"/><stop offset=".6" stop-color="#E0541A" stop-opacity=".08"/><stop offset="1" stop-color="#E0541A" stop-opacity="0"/></radialGradient>
<linearGradient id="hWood" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#B07A4C"/><stop offset=".45" stop-color="#6E4428"/><stop offset="1" stop-color="#2E1A0E"/></linearGradient>
<linearGradient id="hSteel" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#9AA4B6"/><stop offset=".35" stop-color="#4E5562"/><stop offset="1" stop-color="#1B1E25"/></linearGradient>
<linearGradient id="smithShade" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#000" stop-opacity=".05"/><stop offset="1" stop-color="#000" stop-opacity=".6"/></linearGradient>
<linearGradient id="fadeDown" x1="0" y1="0" x2="0" y2="1"><stop offset="0" style="stop-color:var(--night);stop-opacity:0"/><stop offset="1" style="stop-color:var(--night);stop-opacity:1"/></linearGradient>
<linearGradient id="hazeG" x1="0" y1="0" x2="0" y2="1"><stop offset="0" style="stop-color:var(--haze-a)"/><stop offset=".62" style="stop-color:var(--haze-b)"/><stop offset="1" style="stop-color:var(--haze-a)"/></linearGradient>
<linearGradient id="floorFadeG" x1="0" y1="0" x2="0" y2="1"><stop offset="0" style="stop-color:var(--floor-fade);stop-opacity:.9"/><stop offset="1" style="stop-color:var(--floor-fade);stop-opacity:0"/></linearGradient>
<linearGradient id="heat" x1="0" x2="600" y1="0" y2="0" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#3D6BFF"/><stop offset=".22" stop-color="#9FC0FF"/><stop offset=".4" stop-color="#FFF1C8"/><stop offset=".6" stop-color="#FFC24D"/><stop offset=".8" stop-color="#FF8A1F"/><stop offset="1" stop-color="#E2481A"/></linearGradient>
<linearGradient id="bedG" x1="346" x2="1056" y1="0" y2="0" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#5A1408"/><stop offset=".25" stop-color="#C8381E"/><stop offset=".43" stop-color="#FFB03C"/><stop offset=".5" stop-color="#FFE9B0"/><stop offset=".6" stop-color="#FFB03C"/><stop offset=".8" stop-color="#C8381E"/><stop offset="1" stop-color="#5A1408"/></linearGradient>
<radialGradient id="howLit" cx="120" cy="720" r="560" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#FF8A2A" stop-opacity=".42"/><stop offset=".55" stop-color="#E0541A" stop-opacity=".1"/><stop offset="1" stop-color="#E0541A" stop-opacity="0"/></radialGradient>
<linearGradient id="ingot" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#FFD27A"/><stop offset=".3" stop-color="#FF8A1F"/><stop offset="1" stop-color="#6A2410"/></linearGradient>
<linearGradient id="ingotHot" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#EAF2FF"/><stop offset=".4" stop-color="#9FC0FF"/><stop offset="1" stop-color="#2448B8"/></linearGradient>
<radialGradient id="hearthRad" cx="650" cy="404" r="520" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#FFB060" stop-opacity=".46"/><stop offset=".3" stop-color="#E0541A" stop-opacity=".2"/><stop offset=".62" stop-color="#3A0E06" stop-opacity=".18"/><stop offset="1" stop-color="#000" stop-opacity=".6"/></radialGradient>
<linearGradient id="seamCast" x1="0" y1="151" x2="0" y2="176" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#FF8A3A" stop-opacity=".6"/><stop offset=".4" stop-color="#E0541A" stop-opacity=".22"/><stop offset="1" stop-color="#E0541A" stop-opacity="0"/></linearGradient>
<linearGradient id="ironRein" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#5A5A62"/><stop offset=".45" stop-color="#26272D"/><stop offset="1" stop-color="#0E0F12"/></linearGradient>
<linearGradient id="billetG" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#FFD870"/><stop offset=".3" stop-color="#FFA43A"/><stop offset=".7" stop-color="#F06A1C"/><stop offset="1" stop-color="#B8301A"/></linearGradient>
<linearGradient id="leatherB" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#8A4A2C"/><stop offset=".4" stop-color="#4A2216"/><stop offset="1" stop-color="#1E0C08"/></linearGradient>
<linearGradient id="woodTop" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#A87448"/><stop offset=".45" stop-color="#5E3A22"/><stop offset="1" stop-color="#2E1A0E"/></linearGradient>
<linearGradient id="fBlue" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#3D6BFF" stop-opacity="0"/><stop offset=".35" stop-color="#4C78FF" stop-opacity=".85"/><stop offset=".8" stop-color="#9FC0FF"/><stop offset="1" stop-color="#EAF2FF"/></linearGradient>
<linearGradient id="fCopper" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#1FA87A" stop-opacity="0"/><stop offset=".3" stop-color="#2FC88E" stop-opacity=".8"/><stop offset=".75" stop-color="#7CF0C4"/><stop offset="1" stop-color="#E6FFF4"/></linearGradient>
<linearGradient id="brassPlate" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#FFE6A8"/><stop offset=".18" stop-color="#E9C98A"/><stop offset=".6" stop-color="#B9873F"/><stop offset="1" stop-color="#7A5420"/></linearGradient>
<linearGradient id="temper" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#23262E"/><stop offset=".45" stop-color="#3C404A"/><stop offset=".68" stop-color="#6E5E3E"/><stop offset=".8" stop-color="#6A4A6A"/><stop offset=".9" stop-color="#3A5688"/><stop offset="1" stop-color="#262A33"/></linearGradient>
<linearGradient id="heatbar" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#4A160C"/><stop offset=".35" stop-color="#A8301A"/><stop offset=".65" stop-color="#F06A1C"/><stop offset=".86" stop-color="#FFC24D"/><stop offset="1" stop-color="#FFF6E0"/></linearGradient>
<linearGradient id="faceFall" x1="80" x2="222" y1="0" y2="0" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#FFB060"/><stop offset=".3" stop-color="#FF9A4A" stop-opacity=".8"/><stop offset=".75" stop-color="#FF8A3A" stop-opacity=".15"/><stop offset="1" stop-color="#FF8A3A" stop-opacity="0"/></linearGradient>
<linearGradient id="shadG" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#000" stop-opacity=".55"/><stop offset=".5" stop-color="#000" stop-opacity=".25"/><stop offset="1" stop-color="#000" stop-opacity="0"/></linearGradient>
<linearGradient id="billetH" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#FFB444"/><stop offset=".34" stop-color="#FFD866"/><stop offset=".62" stop-color="#FFF2B4"/><stop offset=".78" stop-color="#FFFCEC"/><stop offset=".9" stop-color="#FFC050"/><stop offset="1" stop-color="#B8361A"/></linearGradient>
<radialGradient id="faceGlow"><stop offset="0" stop-color="#FFC060" stop-opacity=".9"/><stop offset=".45" stop-color="#FF8A2A" stop-opacity=".45"/><stop offset="1" stop-color="#FF6A1A" stop-opacity="0"/></radialGradient>
<linearGradient id="billetHTop" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#FFD27A"/><stop offset=".4" stop-color="#FFEFB0"/><stop offset=".76" stop-color="#FFFFF6"/><stop offset=".9" stop-color="#FFD880"/><stop offset="1" stop-color="#D2501E"/></linearGradient>
<linearGradient id="billetDull" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#9A2E16"/><stop offset=".6" stop-color="#6A1C10"/><stop offset="1" stop-color="#2E120C"/></linearGradient>
<linearGradient id="hillG" x1="0" y1="0" x2="0" y2="1"><stop offset="0" style="stop-color:var(--hill-a)"/><stop offset="1" style="stop-color:var(--hill-b)"/></linearGradient>
<linearGradient id="roadG" x1="0" y1="0" x2="0" y2="1"><stop offset="0" style="stop-color:var(--joint)"/><stop offset="1" style="stop-color:var(--hill-b)"/></linearGradient>
<linearGradient id="fogBand" x1="0" y1="0" x2="0" y2="1"><stop offset="0" style="stop-color:var(--fog);stop-opacity:0"/><stop offset=".55" style="stop-color:var(--fog);stop-opacity:.55"/><stop offset="1" style="stop-color:var(--fog);stop-opacity:0"/></linearGradient>
<radialGradient id="poolG"><stop offset="0" stop-color="#FFB060" stop-opacity=".42"/><stop offset=".5" stop-color="#FF8A3A" stop-opacity=".16"/><stop offset="1" stop-color="#FF7A2A" stop-opacity="0"/></radialGradient>
<radialGradient id="bloomG"><stop offset="0" stop-color="#FFE7A8" stop-opacity=".55"/><stop offset=".3" stop-color="#FFB060" stop-opacity=".2"/><stop offset="1" stop-color="#FF8A3A" stop-opacity="0"/></radialGradient>
<linearGradient id="adit" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#0B0A10"/><stop offset=".7" stop-color="#1A1410"/><stop offset="1" stop-color="#2A1C12"/></linearGradient>
<linearGradient id="waterR" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#5A6A8A"/><stop offset=".45" stop-color="#1A2232"/><stop offset="1" stop-color="#0C1018"/></linearGradient>
<linearGradient id="doorLit" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#FFB050"/><stop offset=".6" stop-color="#FFD27A"/><stop offset="1" stop-color="#FFE7A8"/></linearGradient>
<linearGradient id="doorCast" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#FFC060" stop-opacity=".45"/><stop offset="1" stop-color="#FF8A3A" stop-opacity="0"/></linearGradient>
<linearGradient id="towerStone" x1="0" y1="0" x2="1" y2="0"><stop offset="0" style="stop-color:var(--tw-a)"/><stop offset=".5" style="stop-color:var(--tw-b)"/><stop offset="1" style="stop-color:var(--tw-c)"/></linearGradient>
<linearGradient id="towerHaze" x1="0" y1="0" x2="0" y2="1"><stop offset="0" style="stop-color:var(--fog);stop-opacity:.1"/><stop offset=".7" style="stop-color:var(--fog);stop-opacity:.22"/><stop offset="1" style="stop-color:var(--fog);stop-opacity:.34"/></linearGradient>
<linearGradient id="ridgeEdge" x1="232" y1="0" x2="612" y2="0" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#8FA6D6" stop-opacity=".3"/><stop offset=".45" stop-color="#FFB070" stop-opacity=".6"/><stop offset=".62" stop-color="#FFB070" stop-opacity=".45"/><stop offset="1" stop-color="#8FA6D6" stop-opacity=".25"/></linearGradient>
<linearGradient id="billetW" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#E8661E"/><stop offset=".45" stop-color="#FF8A2C"/><stop offset=".76" stop-color="#FFA23E"/><stop offset=".9" stop-color="#E8601C"/><stop offset="1" stop-color="#7A2010"/></linearGradient>
<linearGradient id="billetWTop" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#FF9A44"/><stop offset=".76" stop-color="#FFC874"/><stop offset=".9" stop-color="#FF8A36"/><stop offset="1" stop-color="#9A2E14"/></linearGradient>
<radialGradient id="hotSpot"><stop offset="0" stop-color="#FFFFF4" stop-opacity=".9"/><stop offset=".5" stop-color="#FFF6D0" stop-opacity=".45"/><stop offset="1" stop-color="#FFE7A0" stop-opacity="0"/></radialGradient>
<radialGradient id="knuckleG"><stop offset="0" stop-color="#9A7A5E" stop-opacity=".75"/><stop offset=".6" stop-color="#6A503C" stop-opacity=".25"/><stop offset="1" stop-color="#5A4434" stop-opacity="0"/></radialGradient>
<radialGradient id="barGlow" cx=".68" cy=".5" r=".6"><stop offset="0" stop-color="#FFC86A" stop-opacity=".62"/><stop offset=".55" stop-color="#FF7A2A" stop-opacity=".22"/><stop offset="1" stop-color="#FF7A2A" stop-opacity="0"/></radialGradient>
<radialGradient id="hotGlow"><stop offset="0" stop-color="#FF9A3A" stop-opacity=".75"/><stop offset=".6" stop-color="#FF6A1A" stop-opacity=".25"/><stop offset="1" stop-color="#FF6A1A" stop-opacity="0"/></radialGradient>
<linearGradient id="hSteelW" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#C8A890"/><stop offset=".3" stop-color="#7A7274"/><stop offset=".7" stop-color="#3E424C"/><stop offset="1" stop-color="#1E2128"/></linearGradient>
<linearGradient id="gloveG" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#5A4434"/><stop offset=".45" stop-color="#34261E"/><stop offset="1" stop-color="#140E0B"/></linearGradient>
<!-- the heat haze over the fire: three still noises the hammer cycles through -->
<filter id="hz0" filterUnits="userSpaceOnUse" x="-50" y="-100" width="410" height="180" color-interpolation-filters="sRGB"><feTurbulence type="fractalNoise" baseFrequency=".011 .05" numOctaves="1" seed="3"/><feDisplacementMap in="SourceGraphic" scale="3" xChannelSelector="R" yChannelSelector="G"/></filter><filter id="hz1" filterUnits="userSpaceOnUse" x="-50" y="-100" width="410" height="180" color-interpolation-filters="sRGB"><feTurbulence type="fractalNoise" baseFrequency=".011 .05" numOctaves="1" seed="10"/><feDisplacementMap in="SourceGraphic" scale="3" xChannelSelector="R" yChannelSelector="G"/></filter><filter id="hz2" filterUnits="userSpaceOnUse" x="-50" y="-100" width="410" height="180" color-interpolation-filters="sRGB"><feTurbulence type="fractalNoise" baseFrequency=".011 .05" numOctaves="1" seed="17"/><feDisplacementMap in="SourceGraphic" scale="3" xChannelSelector="R" yChannelSelector="G"/></filter>
<linearGradient id="cheekG" x1="0" y1="1" x2=".8" y2=".15"><stop offset="0" stop-color="#FFB070" stop-opacity=".5"/><stop offset=".3" stop-color="#FF8A3A" stop-opacity=".3"/><stop offset=".62" stop-color="#FF8A3A" stop-opacity=".06"/><stop offset="1" stop-color="#FF8A3A" stop-opacity="0"/></linearGradient>
<linearGradient id="skinG" x1="416.3" y1="-200.1" x2="458" y2="-158.4" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#2E1A14"/><stop offset=".35" stop-color="#5E3A2C"/><stop offset=".55" stop-color="#8A5A44"/><stop offset=".82" stop-color="#B07858"/><stop offset="1" stop-color="#D49670"/></linearGradient>
<linearGradient id="sleeveG" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#5E5446"/><stop offset=".5" stop-color="#3A332A"/><stop offset="1" stop-color="#1E1A15"/></linearGradient>
<linearGradient id="rollG" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#A8967A"/><stop offset=".5" stop-color="#6E6250"/><stop offset="1" stop-color="#3A322A"/></linearGradient>
<linearGradient id="thumbG" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#5A4030"/><stop offset="1" stop-color="#2A1C14"/></linearGradient>
<linearGradient id="cuffG" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#3A2214"/><stop offset=".55" stop-color="#5E3A22"/><stop offset="1" stop-color="#8A5430"/></linearGradient>
<linearGradient id="armG" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#5E3C26"/><stop offset=".5" stop-color="#3A2416"/><stop offset="1" stop-color="#1A0E08"/></linearGradient>
<linearGradient id="waterM" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#2A2232"/><stop offset=".35" stop-color="#0E0C16"/><stop offset="1" stop-color="#050408"/></linearGradient>
<linearGradient id="temper3" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#2A2B30"/><stop offset=".5" stop-color="#4A4640"/><stop offset=".78" stop-color="#8A7444"/><stop offset=".92" stop-color="#9A6A3A"/><stop offset="1" stop-color="#3A3430"/></linearGradient>
<linearGradient id="temper2" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#26272E"/><stop offset=".5" stop-color="#433E4C"/><stop offset=".78" stop-color="#6E4A6A"/><stop offset=".92" stop-color="#5A3E78"/><stop offset="1" stop-color="#2E2A36"/></linearGradient>
<linearGradient id="temper1" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#24262E"/><stop offset=".5" stop-color="#3A4050"/><stop offset=".78" stop-color="#3A5688"/><stop offset=".92" stop-color="#4A70A8"/><stop offset="1" stop-color="#262C3A"/></linearGradient>
<linearGradient id="temper0" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#26282E"/><stop offset=".6" stop-color="#3C3F48"/><stop offset=".9" stop-color="#55585F"/><stop offset="1" stop-color="#2A2C32"/></linearGradient>
<linearGradient id="billetQ" x1="56" x2="188" y1="0" y2="0" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#121014"/><stop offset=".45" stop-color="#2A1412"/><stop offset=".75" stop-color="#6A1A10"/><stop offset="1" stop-color="#B8301A"/></linearGradient>
<linearGradient id="billetTopQ" x1="58" x2="190" y1="0" y2="0" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#1A181C"/><stop offset=".45" stop-color="#2E2220"/><stop offset=".78" stop-color="#5A2418"/><stop offset="1" stop-color="#A0402A"/></linearGradient>
<radialGradient id="vapor"><stop offset="0" style="stop-color:var(--vapor);stop-opacity:.9"/><stop offset=".5" style="stop-color:var(--vapor);stop-opacity:.42"/><stop offset="1" style="stop-color:var(--vapor);stop-opacity:0"/></radialGradient>
<radialGradient id="vaporG"><stop offset="0" style="stop-color:var(--vapor-g);stop-opacity:.9"/><stop offset=".5" style="stop-color:var(--vapor-g);stop-opacity:.42"/><stop offset="1" style="stop-color:var(--vapor-g);stop-opacity:0"/></radialGradient>
<linearGradient id="billetHotQ" x1="100" x2="188" y1="0" y2="0" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#8A1E10" stop-opacity="0"/><stop offset=".5" stop-color="#E2481A"/><stop offset=".85" stop-color="#FF8A2A"/><stop offset="1" stop-color="#FFC24D"/></linearGradient>
<radialGradient id="ridgeLit" cx="440" cy="410" r="460" gradientUnits="userSpaceOnUse"><stop offset="0" style="stop-color:var(--ridge-a)"/><stop offset=".45" style="stop-color:var(--ridge-m)"/><stop offset="1" style="stop-color:var(--ridge-b)"/></radialGradient>
<linearGradient id="moonLit" x1="1" y1="0" x2="0" y2="0"><stop offset="0" stop-color="#F4ECFF"/><stop offset=".55" stop-color="#D2C0F4"/><stop offset="1" stop-color="#8A72C8"/></linearGradient>
<linearGradient id="tipCore" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#FFF6E0" stop-opacity="0"/><stop offset="1" stop-color="#FFF6E0"/></linearGradient>
<radialGradient id="tipGlow"><stop offset="0" stop-color="#FFE2A0" stop-opacity=".8"/><stop offset=".6" stop-color="#FF9A3A" stop-opacity=".25"/><stop offset="1" stop-color="#FF9A3A" stop-opacity="0"/></radialGradient>
<linearGradient id="bankFall" x1="0" y1="150" x2="0" y2="440" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#FF7A2A" stop-opacity=".16"/><stop offset=".4" stop-color="#000" stop-opacity=".2"/><stop offset="1" stop-color="#000" stop-opacity=".6"/></linearGradient>
<linearGradient id="dayShadeG" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#2A2E52" stop-opacity=".3"/><stop offset="1" stop-color="#2A2E52" stop-opacity="0"/></linearGradient>
<linearGradient id="ashG" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#6E6A72"/><stop offset="1" stop-color="#2A272E"/></linearGradient>
'''

# Moons comes from the same export the Rune page draws (tower/genesis.json, read only): every count and stage below is the export's
NUM = 'zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty'.split()
def moons_from_export():
    d = json.load(open(os.path.join(HERE, '..', 'tower', 'genesis.json')))['moonshots']['rows']
    wild = sorted((r for r in d if r['wild'] >= 4), key=lambda r: (-r['wild'], r['id']))
    stage = lambda r: 'kill test' if r['stage'] == 'kill' else r['stage']
    return d, wild, stage

def main():
    W, H = hero.W, hero.H
    rows, wild, stage = moons_from_export()
    fig_how, how_y = stations.how()
    hb = hero.HAMMER_BOX; px, py, pr = hero.POSE_RAISED
    hammer = (f'<svg class="hammer" id="hammer" viewBox="{hb[0]} {hb[1]} {hb[2]} {hb[3]}" aria-hidden="true" '
              f'style="left:{hb[0] / W * 100:.3f}%;top:{hb[1] / H * 100:.3f}%;width:{hb[2] / W * 100:.3f}%;height:{hb[3] / H * 100:.3f}%;'
              f'transform-origin:{-hb[0] / hb[2] * 100:.3f}% {-hb[1] / hb[3] * 100:.3f}%;transform:translate({px / hb[2] * 100:.3f}%,{py / hb[3] * 100:.3f}%) rotate({pr}deg)">{hero.hammer()}</svg>')
    far_svg, rims = hero.far()
    pool, shad = hero.lightrig_floor()
    import marks
    foot_mark = f'<svg class="mark mk" viewBox="0 0 256 256" role="img" aria-label="The Sampo mark: a steel anvil on a rune-cut stone bound with nine rivets, a fire of many colours behind it, one star">{marks.mark(True, lite=True)}</svg>'
    rep = {
        'ICON': uri('icon-16.svg'), 'LOGO_SIMPLE': uri('logo-simple.svg'), 'FOOT_MARK': foot_mark, 'MARK_CSS': marks.scoped_css(),
        'DEFS': art.DEFS + art.LOGO_FLAME_DEFS + EXTRA_DEFS,
        'SKY': hero.sky_static(), 'FAR': far_svg, 'RIMS': rims, 'FLOOR': hero.floor(), 'HEARTH_BACK': hero.hearth_back(),
        'STATIC_FLAMES': hero.static_flames(), 'POOL': pool, 'SHAD': shad, 'BILLET': hero.billet(), 'TONGS': hero.tongs_ov(), 'LITE_ANV': hero.lightrig_fore(), 'STEAM': hero.steam_ov(),
        'HEARTH_FRONT': hero.hearth_front(),
        'BELLOWS': art.ov(W, H, (150, 340, 240, 470), hero.bellows_static(), 'bw', '', 'id="bellows"'),
        'ANVIL': hero.anvil_stone(), 'OVERLAYS': hero.overlays(), 'STATIC_FX': hero.static_fx(), 'HAMMER': hammer,
        'TROUGH': hero.trough(), 'NEAR': hero.near(),
        'FIG_HOW': fig_how, 'FIG_FOOT': stations.banked(), 'FIG_SMITH': stations.smith(), 'FIG_RUNE': stations.rune(), 'FIG_WARD': stations.ward(),
        'MOON_LIST': ''.join(f'<li{" class=\"kt\"" if r["stage"] == "kill" else ""}><b>{r["id"]}</b><span>{html.escape(r["title"])}<i>wild {r["wild"]} of 5 · {stage(r)}</i></span></li>' for r in wild),
        'MOONS_H2': f'{len(rows)} moonshots over the forge, <em>the wildest first</em>',
        'MOONS_INTRO': (f'Moons is the moonshot catalogue: ideas too large for a task, each with what it needs first. It holds {len(rows)}; '
                        f'these are the {NUM[len(wild)]} rated 4 or 5 of 5 for wildness, wildest first. '
                        f'{sum(r["stage"] == "idea" for r in rows)} of the {len(rows)} are still ideas, a thin sliver on the chart; '
                        + ', '.join(r['id'] for r in rows if r['stage'] == 'kill').replace(', ', ' and ', 1) + ' stand at their kill test, a crescent. None is built, so no moon is full. '
                        '<a class="more" href="tower/index.html#moonshots">All of them in Rune&#x27;s sky</a>'),
        'MOONS_CAP': f'The {NUM[len(wild)]} wildest of {len(rows)} moonshots along the ecliptic over the forge, wildest first, each drawn at its stage: ideas a thin sliver, kill tests a crescent, the wild 5s brighter. Otava stands above them, its pointers on the pole.',
        'CHART': stations.moons_chart([(int(r['id'][2:]), r['wild'], r['stage']) for r in wild], len(rows)),
        'JS': f'const HB={json.dumps(list(hero.HAMMER_BOX[2:]))}, RZ={json.dumps(list(hero.POSE_RAISED))};\n' + open(os.path.join(HERE, 'page.js')).read(),
    }
    import re  # N-15: the chart's accessible name must agree with the export it is drawn from
    lab = re.search(r'role="img" aria-label="([^"]*)"', rep['CHART']).group(1)
    assert f'wildest of {len(rows)} moonshots' in lab and NUM[len(wild)] + ' wildest' in lab, lab
    assert set(re.findall(r'M-\d+', lab)) == {r['id'] for r in wild if r['stage'] == 'kill'}, lab
    assert not re.search(r'struck|numbered 01 to', lab), lab
    css = open(os.path.join(HERE, 'style.css')).read().replace('{{MARK_CSS}}', rep.pop('MARK_CSS')).replace('{{SMITH_CSS}}', stations.smith_css() + stations.how_css())
    js = rep.pop('JS')
    page = open(os.path.join(HERE, 'page.html')).read()
    for k, v in rep.items():
        assert '{{' + k + '}}' in page, k
        page = page.replace('{{' + k + '}}', v)
    # T-4868: the readings (the Ward's blocks and kinds, lanes merged, the Record's gauges, ledger and date) are filled from
    # site/data/stats.json by site/build.py:fill_record (T-4842), never here: these keys stay braces in the page this writes
    DATA_KEYS = ('lanes', 'blocks', 'asof', 'kinds', 'gauges', 'ledger')
    left = page.replace('{{CSS}}', '').replace('{{JS}}', '')
    for k in DATA_KEYS:
        assert '{{' + k + '}}' in left, 'page.html lost the data placeholder ' + k
        left = left.replace('{{' + k + '}}', '')
    assert '{{' not in left, left[left.find('{{'):left.find('{{') + 40]
    # one page, self-contained but for the fonts; and the same page split as the repo will carry it (site/)
    whole = page.replace('{{CSS}}', css).replace('{{JS}}', js)
    open(os.path.join(HERE, '..', 'index.html'), 'w').write(whole)
    site = os.path.join(HERE, '..', 'site'); os.makedirs(os.path.join(site, 'fonts'), exist_ok=True)
    split = page.replace('<style>\n{{CSS}}\n</style>', '<link rel="stylesheet" href="style.css">').replace('<script>\n{{JS}}\n</script>', '<script src="site.js"></script>')
    kept = split
    for k in DATA_KEYS: kept = kept.replace('{{' + k + '}}', '')
    assert '{{' not in kept, 'split page still has a placeholder: ' + kept[kept.find('{{'):kept.find('{{') + 40]  # a {{CSS}} leak
    open(os.path.join(site, 'index.html'), 'w').write(split); open(os.path.join(site, 'style.css'), 'w').write(css); open(os.path.join(site, 'site.js'), 'w').write(js)
    for fn in os.listdir(os.path.join(HERE, 'fonts')): shutil.copy(os.path.join(HERE, 'fonts', fn), os.path.join(site, 'fonts', fn))
    print('index.html', len(whole.encode()) // 1024, 'KB; site/ index.html', len(split.encode()) // 1024, 'KB, style.css', len(css.encode()) // 1024, 'KB, site.js', len(js.encode()) // 1024, 'KB')

if __name__ == '__main__':
    main()
