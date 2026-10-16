-- Synthetic Salesforce / OCB reference data for the lab. No customer data.
set search_path = al_poc;

insert into accounts (sold_to_code, name, email_domain, contact_email, sales_org, plant) values
  ('0001004512', 'Ateliers Durand SAS',      'ateliers-durand.example', 'achats@ateliers-durand.example', 'FR01', 'FR07'),
  ('0001007830', 'Brauerei Kessler GmbH',    'kessler-brau.example',    'einkauf@kessler-brau.example',   'DE01', 'DE12'),
  ('0001009921', 'Clinique Saint-Martin',    'clinique-stmartin.example','pharmacie@clinique-stmartin.example', 'FR01', 'FR07')
on conflict do nothing;

insert into partner_roles (sold_to_code, ship_to_code, role, name, street, city, postal_code, country) values
  ('0001004512', '0002204512', 'SH', 'Ateliers Durand - Atelier principal', '14 rue des Forges', 'Saint-Etienne', '42000', 'FR'),
  ('0001004512', '0002204513', 'SH', 'Ateliers Durand - Depot Nord',        '3 impasse du Canal', 'Saint-Etienne', '42000', 'FR'),
  ('0001007830', '0002207830', 'SH', 'Brauerei Kessler - Werk',             'Brauereistrasse 8',  'Bamberg',       '96047', 'DE'),
  ('0001009921', '0002209921', 'SH', 'Clinique Saint-Martin - Pharmacie',   '22 avenue Pasteur',  'Lyon',          '69003', 'FR')
on conflict do nothing;

insert into product_catalog (product_code, name, aliases, unit, category, is_cylinder) values
  ('ARCAL-1-B50',  'ARCAL 1 welding shield gas, B50 cylinder',   '{"arcal 1","arcal1 b50","arcal 1 b50"}',       'CYL', 'welding',   true),
  ('ARCAL-14-B50', 'ARCAL 14 welding shield gas, B50 cylinder',  '{"arcal 14","arcal14"}',                        'CYL', 'welding',   true),
  ('OXY-IND-B50',  'Oxygen industrial, B50 cylinder',            '{"oxygene industriel b50","oxygen b50","O2 B50"}','CYL', 'industrial', true),
  ('ACET-B40',     'Acetylene, B40 cylinder',                    '{"acetylene","acetylene b40"}',                 'CYL', 'industrial', true),
  ('CO2-FOOD-B20', 'Carbon dioxide food grade ALIGAL 2, B20',    '{"co2 food","aligal 2","aligal2 b20","kohlensaeure b20"}', 'CYL', 'food', true),
  ('N2-FOOD-B50',  'Nitrogen food grade ALIGAL 1, B50',          '{"aligal 1","n2 food b50"}',                    'CYL', 'food', true),
  ('OXY-MED-B11',  'Oxygen medicinal, B11 cylinder',             '{"oxygene medical b11","medical oxygen b11"}',  'CYL', 'medical',   true),
  ('OXY-MED-B02',  'Oxygen medicinal, B02 cylinder',             '{"oxygene medical b02","medical oxygen b02"}',  'CYL', 'medical',   true),
  ('REG-OXY-200',  'Oxygen regulator 200 bar',                   '{"detendeur oxygene","oxygen regulator"}',     'EA',  'equipment', false)
on conflict do nothing;

insert into cut_offs (sales_org, plant, cutoff_local_time, tz) values
  ('FR01', 'FR07', '23:59', 'Europe/Paris'),
  ('DE01', 'DE12', '09:00', 'Europe/Berlin')
on conflict do nothing;
