# Northwind Telecom — Setting up your fibre router

*Synthetic document. Northwind Telecom is a fictional operator invented for testing; every figure
below is made up and describes no real product, tariff or person.*

## In the box

The router, a power adapter, a one-metre fibre patch cable with green connectors, a one-metre
ethernet cable, and a card carrying the default Wi-Fi name and password.

## Step 1 — Connect the fibre

Remove the dust cap from the green connector and push it into the port marked PON until it clicks.
Do not force it; the connector only fits one way round. Leave the dust cap on any port you are not
using.

## Step 2 — Power on

Connect the adapter and press the power button on the back. The POWER light turns solid green
immediately. The PON light blinks while the line registers and turns solid green within two minutes.
The LOS light must stay off; a red LOS light means no light is reaching the router.

## Step 3 — Join the Wi-Fi

Use the network name and password printed on the card. Two networks are broadcast, one ending in
`-2G` and one ending in `-5G`. Use `-5G` near the router for speed and `-2G` further away for range.

## Step 4 — Change the defaults

Open `http://192.168.1.1` in a browser, sign in with the admin password printed on the underside of
the router, and change both the admin password and the Wi-Fi password. The router prompts for this
on first sign-in.

## Light reference

| Light | Meaning |
|---|---|
| POWER solid green | the router has power |
| PON blinking | the line is registering |
| PON solid green | the line is registered |
| LOS red | no light on the fibre — call support |
| LAN green | a device is connected by cable |

## If the PON light never turns solid

Check that the fibre connector is fully seated and that the cable is not bent tighter than a
10-centimetre radius. If both are fine and LOS is red, the fault is on the line and needs a visit.
